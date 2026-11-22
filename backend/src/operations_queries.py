"""Business queries over authoritative project records."""

VIEWS = {
    'rework-supply': (
        '返工物料追溯',
        '沿返工单核对领用 SKU、库位与分段复测覆盖，用于班末交接和缺证补录。',
        ['批次', '返工单', 'SKU', '库位', '触发原因', '状态', '通过件数', '失败记录', '下一步'],
        """
SELECT l.code,r.id,r.sku,COALESCE(s.bin,'未配置'),r.reason,r.state,
       COALESCE((SELECT sum(t.last_unit-t.first_unit+1)::int FROM rework_retest t
                 WHERE t.issue_id=r.id AND t.outcome='passed'),0),
       COALESCE((SELECT count(*)::int FROM rework_retest t
                 WHERE t.issue_id=r.id AND t.outcome='failed'),0),
       CASE WHEN s.sku IS NULL THEN '核对库位' WHEN r.issued_at IS NULL THEN '等待领料'
            WHEN COALESCE((SELECT sum(t.last_unit-t.first_unit+1) FROM rework_retest t
                           WHERE t.issue_id=r.id AND t.outcome='passed'),0) < r.required_units
                 THEN '等待补录复测' ELSE '复测已覆盖' END
FROM rework_issue r JOIN pack_lot l ON l.id=r.lot_id LEFT JOIN material_stock s ON s.sku=r.sku ORDER BY r.id DESC
""",
    ),
}
