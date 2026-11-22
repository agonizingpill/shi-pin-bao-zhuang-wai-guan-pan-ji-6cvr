import hashlib
import json

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .inspection import (audit, latest_grade, latest_measure, lot_row, missing_channels,
                         rework_progress, set_state)
from .model import predict
from .schemas import CHANNELS, VERIFICATION_LIMITS


def sign_grade(db, lot_id, data, actor):
    lot = lot_row(db, lot_id, lock=True)
    if lot['state'] != 'review':
        raise HTTPException(409, '需要等待模型判级完成')
    measure = latest_measure(db, lot_id)
    decision = latest_grade(db, measure['id'])
    if not decision or decision['state'] != 'predicted':
        raise HTTPException(409, '没有可签核的模型判级')
    if db.execute("SELECT 1 FROM defect_mark WHERE measure_id=%s AND state='open'", (measure['id'],)).fetchone():
        raise HTTPException(409, '当前样本还有待复核的缺陷标记')
    override = data.grade != decision['model_grade']
    rework = override or data.grade != 'A'
    trigger = 'override' if override else 'out_of_grade'
    if rework and (not data.reason or not data.rework_units or data.rework_units > lot['units']):
        raise HTTPException(422, '改判或非 A 级必须记录理由和返工数量，数量不得超过批次总量')
    if not rework and data.rework_units is not None:
        raise HTTPException(422, 'A 级同判直接放行，不领取返工膜材')
    signed = db.execute('''UPDATE grade_decision SET state='signed',final_grade=%s,reason=%s,
                           reviewed_by=%s,reviewed_at=now() WHERE id=%s RETURNING *''',
                        (data.grade, data.reason, actor, decision['id'])).fetchone()
    audit(db, lot_id, actor, 'grade_signed', model=decision['model_grade'], final=data.grade,
          override=override, reason=data.reason, decision_id=decision['id'])
    if rework:
        row = db.execute('''INSERT INTO rework_issue(lot_id,decision_id,sku,required_units,reason,trigger)
                            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',
                         (lot_id, decision['id'], lot['film_sku'], data.rework_units, data.reason, trigger)).fetchone()
        audit(db, lot_id, actor, 'rework_requested', issue_id=row['id'], sku=row['sku'],
              quantity=row['required_units'], trigger=trigger)
    set_state(db, lot, 'rework' if rework else 'approved')
    return signed


def issue_row(db, lot_id, expected):
    row = db.execute('SELECT * FROM rework_issue WHERE lot_id=%s FOR UPDATE', (lot_id,)).fetchone()
    if not row or row['state'] != expected:
        raise HTTPException(409, '返工单状态与此操作不匹配')
    return row


def issue_material(db, lot_id, actor):
    lot_row(db, lot_id, lock=True)
    issue = issue_row(db, lot_id, 'requested')
    stock = db.execute('''UPDATE material_stock SET on_hand=on_hand-%s WHERE sku=%s AND on_hand >= %s
                          RETURNING on_hand''', (issue['required_units'], issue['sku'], issue['required_units'])).fetchone()
    if not stock:
        raise HTTPException(409, '库位余量不足，返工单仍待领料')
    row = db.execute("UPDATE rework_issue SET state='issued',issued_by=%s,issued_at=now() WHERE id=%s RETURNING *",
                     (actor, issue['id'])).fetchone()
    audit(db, lot_id, actor, 'material_issued', sku=issue['sku'], quantity=issue['required_units'], balance=stock['on_hand'])
    return row


def retest_digest(data):
    """Fingerprint one retest attempt so a network retry cannot add a second record."""
    payload = {'sample': data.sample, 'machine': data.machine, 'first_unit': data.first_unit,
               'last_unit': data.last_unit, 'channels': {key: getattr(data, key) for key in CHANNELS}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def retest_rejection(row, progress):
    checks = row['checks'] or {}
    missing = checks.get('missing') or []
    exceeds = checks.get('exceeds') or []
    grade = row['model_grade']
    if checks.get('model_fault'):
        causes = ['模型计算失败']
    else:
        causes = []
        if missing:
            causes.append('通道缺失')
        if exceeds:
            causes.append('读数超限')
        if grade and grade != 'A':
            causes.append(f'模型判定 {grade} 级')
    return {'rejected': True, 'segment_id': row['id'], 'sample': row['sample'],
            'first_unit': row['first_unit'], 'last_unit': row['last_unit'],
            'missing': missing, 'exceeds': exceeds, 'model_grade': grade,
            'message': f"该段复测未通过（{'、'.join(causes) or '读数无效'}），已留存记录，可换样重测",
            'progress': progress}


def verify_rework(db, lot_id, data, actor):
    """Record one numbered segment of a film replacement.

    Segments run from unit 1 to the issued quantity. A segment passes only when all five
    channels are present, within limits, and the model also returns grade A. Failed
    attempts are kept and can be retried with another sample.
    """
    lot = lot_row(db, lot_id, lock=True)
    issue = db.execute('SELECT * FROM rework_issue WHERE lot_id=%s FOR UPDATE', (lot_id,)).fetchone()
    if not issue:
        raise HTTPException(409, '找不到该批次的返工单')
    if data.machine != lot['machine']:
        raise HTTPException(409, '复测机台必须与原批次一致')
    digest = retest_digest(data)
    # The issue row lock above serialises submissions, so an identical retry sees the first record.
    existing = db.execute('SELECT * FROM rework_retest WHERE issue_id=%s AND attempt_key=%s',
                          (issue['id'], digest)).fetchone()
    if existing:
        progress = rework_progress(db, issue)
        if existing['outcome'] == 'failed':
            return retest_rejection(existing, progress)
        return {'segment': existing, 'progress': progress, 'complete': progress['complete'], 'replayed': True}
    if issue['state'] != 'issued':
        raise HTTPException(409, '返工单状态与此操作不匹配')
    total = issue['required_units']
    if data.last_unit > total:
        raise HTTPException(422, f'首尾件号须落在 1–{total} 的领料范围内')
    if db.execute('SELECT 1 FROM rework_retest WHERE issue_id=%s AND sample=%s',
                  (issue['id'], data.sample)).fetchone():
        raise HTTPException(409, '该复测样本号已经登记，请换样重测')
    reading = data.model_dump()
    missing = missing_channels(reading)
    exceeds = [key for key, limit in VERIFICATION_LIMITS.items() if reading[key] is not None and reading[key] > limit]
    grade, evidence = None, None
    fault = False
    if not missing:
        try:
            grade, evidence = predict(reading)
        except Exception:  # a model fault still leaves a failed record, not a lost attempt
            fault = True
    passed = not missing and not exceeds and grade == 'A'
    # Overlaps are checked after the issue lock; only ranges that already passed count.
    if passed:
        overlap = db.execute('''SELECT * FROM rework_retest WHERE issue_id=%s AND outcome='passed'
                                AND first_unit <= %s AND last_unit >= %s ORDER BY id LIMIT 1''',
                             (issue['id'], data.last_unit, data.first_unit)).fetchone()
        if overlap:
            raise HTTPException(409, f"该段与已通过段 #{overlap['first_unit']}–#{overlap['last_unit']} 重叠，"
                                     '不能重复计入，请核对首尾件号')
    checks = {'missing': missing, 'exceeds': exceeds, 'model_grade': grade, 'model_fault': fault}
    row = db.execute('''INSERT INTO rework_retest(issue_id,lot_id,sample,attempt_key,machine,first_unit,last_unit,
                             wrinkle_pct,offset_mm,delta_e,seal_gap_mm,gloss_cv,outcome,checks,model_grade,evidence,
                             note,created_by)
                         VALUES (%(issue_id)s,%(lot_id)s,%(sample)s,%(key)s,%(machine)s,%(first_unit)s,%(last_unit)s,
                             %(wrinkle_pct)s,%(offset_mm)s,%(delta_e)s,%(seal_gap_mm)s,%(gloss_cv)s,%(outcome)s,
                             %(checks)s,%(grade)s,%(evidence)s,%(note)s,%(actor)s)
                         RETURNING *''',
                     {**reading, 'issue_id': issue['id'], 'lot_id': lot_id, 'key': digest,
                      'outcome': 'passed' if passed else 'failed', 'checks': Jsonb(checks), 'grade': grade,
                      'evidence': Jsonb(evidence) if evidence else None, 'actor': actor}).fetchone()
    progress = rework_progress(db, issue)
    if not passed:
        audit(db, lot_id, actor, 'rework_retest_rejected', segment_id=row['id'], sample=row['sample'],
              first_unit=row['first_unit'], last_unit=row['last_unit'], missing=missing, exceeds=exceeds,
              model_grade=grade, note=data.note)
        return retest_rejection(row, progress)
    audit(db, lot_id, actor, 'rework_retest_passed', segment_id=row['id'], sample=row['sample'],
          first_unit=row['first_unit'], last_unit=row['last_unit'], model_grade=grade, note=data.note)
    if progress['complete']:
        db.execute('''UPDATE rework_issue SET state='verified',verified_by=%s,verified_at=now(),
                      verification_note=%s WHERE id=%s''', (actor, data.note, issue['id']))
        set_state(db, lot, 'approved')
        audit(db, lot_id, actor, 'rework_verified', sample=row['sample'], segments=len(progress['segments']),
              replaced_units=total)
        progress = rework_progress(db, issue)
    return {'segment': row, 'progress': progress, 'complete': progress['complete'], 'replayed': False}


def close_lot(db, lot_id, actor):
    lot = lot_row(db, lot_id, lock=True)
    if lot['state'] != 'approved':
        raise HTTPException(409, '批次尚未获得放行，待领料或待复测的返工必须先闭环')
    issue = db.execute('SELECT * FROM rework_issue WHERE lot_id=%s', (lot_id,)).fetchone()
    if issue and not rework_progress(db, issue)['complete']:
        raise HTTPException(409, '返工复测尚未覆盖全部件号，不能关闭批次')
    row = db.execute("UPDATE pack_lot SET state='closed',closed_at=now(),closed_by=%s WHERE id=%s RETURNING *",
                     (actor, lot_id)).fetchone()
    audit(db, lot_id, actor, 'lot_closed', code=lot['code'])
    return row
