import {auditList, channelInputs, empty, heading, lotLink, lotPicker, number, readings, safe, sectionTitle, status, time} from '../ui.js';

function rangeLabel(item) {
  const first = item.first_unit ?? item.first;
  const last = item.last_unit ?? item.last;
  return first === last ? `#${number(first)}` : `#${number(first)}–#${number(last)}`;
}

function coverageBar(progress) {
  const total = progress.required_units;
  const bands = [
    ...progress.segments.filter(segment => segment.outcome === 'passed').map(segment => ({kind:'passed', first:segment.first_unit, last:segment.last_unit})),
    ...progress.gaps.map(gap => ({kind:'gap', first:gap.first, last:gap.last})),
  ].sort((a,b) => a.first - b.first);
  return `<div class="coverage" aria-label="返工件号覆盖 ${progress.passed_units} / ${total} 件">
    <div class="coverage-bar">${bands.map(band => `<span class="${band.kind}" style="--span:${(band.last-band.first+1)/total*100}%" title="${band.kind === 'passed' ? '已通过' : '空档'} ${rangeLabel(band)}"></span>`).join('')}</div>
    <div class="coverage-legend"><span><i class="passed"></i>已通过 ${number(progress.passed_units)} 件</span><span><i class="gap"></i>空档 ${number(total-progress.passed_units)} 件</span><b>${progress.complete ? '全部件号已覆盖' : '仍有空档待复测'}</b></div>
  </div>`;
}

function segmentList(progress, limits) {
  if (!progress.segments.length) return empty('尚无分段复测记录','按领料件数分段登记首尾件号和五通道读数，逐段签核。');
  return `<div class="scroll-x"><table class="segment-table"><thead><tr><th>段</th><th>复测样本</th><th>结果</th><th>模型</th><th>读数 / 说明</th><th>登记</th></tr></thead><tbody>${progress.segments.map(segment => {
    const failed = segment.outcome === 'failed';
    const fails = [segment.checks?.model_fault ? '模型计算失败' : '',
      segment.checks?.missing?.length ? `缺失 ${segment.checks.missing.length} 项` : '',
      segment.checks?.exceeds?.length ? `超限 ${segment.checks.exceeds.length} 项` : '',
      segment.model_grade && segment.model_grade !== 'A' ? `模型 ${segment.model_grade}` : ''].filter(Boolean).join(' · ');
    return `<tr class="${failed ? 'failed-row' : ''}"><td class="mono">${rangeLabel(segment)}</td><td class="mono">${safe(segment.sample)}</td><td><span class="status ${failed ? 'failed' : 'verified'}">${failed ? '未通过' : '通过'}</span></td><td>${segment.model_grade ? `<b class="grade-inline">${safe(segment.model_grade)}</b>` : '—'}</td><td><details class="segment-readings"><summary>${failed ? fails || '读数无效' : '查看读数'}</summary>${readings(segment, limits)}${failed ? `<p class="notice warn">${safe(segment.note)}</p>` : `<p class="muted">${safe(segment.note)}</p>`}</details></td><td><small>${safe(segment.created_by)}<br>${time(segment.created_at)}</small></td></tr>`;
  }).join('')}</tbody></table></div>`;
}

function gapList(progress) {
  if (!progress.complete) {
    return `<p class="notice warn">未覆盖件号：${progress.gaps.map(gap => `<b class="mono">${rangeLabel(gap)}</b>`).join('、')}。全部件号通过前不能关闭批次。</p>`;
  }
  return '<p class="notice">五个通道全部达标且模型判 A 的区段已覆盖全部领料件号，批次可以放行关闭。</p>';
}

export function reworkPage(lots, data, account) {
  const pending = lots.filter(lot => lot.rework_state);
  const r = data?.rework;
  const lot = data?.lot;
  const progress = data?.progress;
  const openGap = progress?.gaps[0];
  return `${heading('包装线 / 返工与放行', '返工领料','按领料件数分段登记首尾件号与复测读数，全部覆盖后交接放行。',pending.length && r ? lotPicker(pending,lot.id,'rework') : '')}
    ${!r ? `${empty('所选批次没有返工需求','主管签核改判或非 A 级时，系统会生成领料单。')}${pending.length ? '<a data-nav href="/rework">查看已有返工单 →</a>' : ''}` : `
    <div class="lot-strip"><div><small>返工单</small><b>RW-${String(r.id).padStart(5,'0')}</b></div><div><small>包装批次</small><b>${safe(lot.code)}</b></div><div><small>产品 / 机台</small><b>${safe(lot.product)} · ${safe(lot.machine)}</b></div>${status(r.state)}<a data-nav href="${lotLink('workbench',lot.id)}">返回判级 →</a></div>
    <div class="rework-grid"><section class="panel ticket">${sectionTitle('备料交接单', r.trigger === 'override' ? '人工改判' : '非 A 级处置')}
      <p class="ticket-cause">${safe(r.reason)}</p><div class="ticket-big"><div class="warehouse-tile"><small>仓储库位</small><strong>${safe(r.bin)}</strong></div><div><small>换膜范围</small><strong>${number(r.required_units)} <span>件</span></strong></div></div>
      <dl class="ticket-meta"><dt>物料</dt><dd>${safe(r.description)}</dd><dt>膜材 SKU</dt><dd class="mono">${safe(r.sku)}</dd><dt>当前库位库存</dt><dd>${number(r.on_hand)} 件</dd><dt>领料操作人</dt><dd>${safe(r.issued_by || '待确认')} ${r.issued_at ? time(r.issued_at) : ''}</dd></dl>
      ${r.state === 'requested' && account.role === 'operator' ? `<button class="primary" data-action="rework/issue" ${r.on_hand < r.required_units ? 'disabled' : ''}>确认领料 · 扣减 ${number(r.required_units)} 件</button>${r.on_hand < r.required_units ? '<p class="notice warn">膜材余量不足，等待仓储补货。</p>' : ''}` : ''}
      ${r.state === 'requested' && account.role !== 'operator' ? '<p class="notice">待线边操作员确认膜材料位并完成出库。</p>' : ''}
      ${r.state !== 'requested' ? `<p class="notice">已领出 ${number(r.required_units)} 件膜材，出库已过账。复测按领料件数从 #1 起编号。</p>` : ''}
    </section><div class="stack"><section class="panel verify-panel">${sectionTitle('分段换膜复测', `${number(progress.passed_units)} / ${number(progress.required_units)} 件已通过`)}
      <ol class="process"><li class="done"><span></span>生成需求</li><li class="${r.state !== 'requested' ? 'done' : 'active'}"><span></span>领料过账</li><li class="${progress.complete ? 'done' : r.state === 'issued' ? 'active' : ''}"><span></span>分段复测</li><li class="${lot.state === 'closed' ? 'done' : lot.state === 'approved' ? 'active' : ''}"><span></span>批次关闭</li></ol>
      ${coverageBar(progress)}
      ${r.state === 'requested' ? '<p class="muted">完成领料后，由主管登记同机台的五通道分段复测。</p>' : ''}
      ${r.state === 'issued' && account.role === 'supervisor' && openGap ? `<form data-action="rework/verify" class="fields"><input type="hidden" name="machine" value="${safe(lot.machine)}"><label>复测样本号<input name="sample" maxlength="64" required placeholder="${safe(lot.code)}-R${String(progress.segments.length+1).padStart(2,'0')}"></label><div class="range-fields"><label>首件号<input name="first_unit" type="number" min="1" max="${progress.required_units}" value="${openGap.first}" required></label><label>尾件号<input name="last_unit" type="number" min="1" max="${progress.required_units}" value="${openGap.last}" required></label></div>${channelInputs({},data.verification_limits)}<label class="full">复测说明<input required minlength="2" maxlength="300" name="note"></label><button class="primary full">签核该段复测</button><p class="field-hint full">五项读数均须达标且模型判 A 才算该段通过；未通过会留存记录，可换样重测。首尾件号不得与已通过段重叠。</p></form>` : ''}
      ${r.state === 'issued' && account.role !== 'supervisor' ? '<p class="notice">等待质检主管登记并签核分段复测。</p>' : ''}
      ${gapList(progress)}
      ${segmentList(progress, data.verification_limits)}
      ${lot.state === 'approved' && account.role === 'supervisor' ? '<button class="primary" data-action="close">关闭批次</button>' : ''}
    </section><section class="panel trace">${sectionTitle('返工履历')}${auditList(data.events.filter(item=>['grade_signed','rework_requested','material_issued','rework_retest_rejected','rework_retest_passed','rework_verified','lot_closed'].includes(item.action)))}</section></div></div>`}`;
}
