-- ============================================================
-- 撤回误收报告（拒收登记）的独立 SQL 用法
-- 与 API POST /reports/retract 等价；拒收登记一旦提交，
-- 触发器 trg_reports_rejection_guard 会拒绝该键的一切 INSERT/改键 UPDATE。
-- ============================================================

-- 1) 撤回：必须在单个事务内完成“加锁 + 删除 + 登记”，保证三者同生共死。
--    咨询锁与补录触发器是同一把（report_key_advisory_lock），撤回与补录严格串行。
BEGIN;

SELECT report_key_advisory_lock('LAB-001', 'RPT-2026-0001');

-- 已登记过则不要重复插入（同键重试应直接查既有登记，见第 3 节）。
-- 唯一索引未生效时同号可能有多行：CTE 内单条 DELETE 全部剔除并精确计数。
WITH del AS (
    DELETE FROM reports
    WHERE  lab_no = 'LAB-001'
      AND  report_no = 'RPT-2026-0001'
    RETURNING id
)
INSERT INTO report_rejections (lab_no, report_no, reason, deleted_rows)
SELECT 'LAB-001', 'RPT-2026-0001', 'lab_retracted', count(*)
FROM   del;

COMMIT;   -- 提交后删除与拒收同时生效；失败/ROLLBACK 则两者皆无，不存在中间态。

-- 2) 撤回之后，下面任何语句都会被数据库拒绝（SQLSTATE R9001），
--    不经过 API 同样生效；其他实验室的同号报告不受影响。
-- INSERT INTO reports(lab_no, report_no, measured)
-- VALUES ('LAB-001', 'RPT-2026-0001', 1024);
--
-- UPDATE reports SET lab_no='LAB-001', report_no='RPT-2026-0001'
-- WHERE lab_no='LAB-001' AND report_no='OTHER';

-- 3) 查询某键的拒收登记（同键重试/对账时使用）。
SELECT * FROM report_rejections
WHERE  lab_no = 'LAB-001' AND report_no = 'RPT-2026-0001';
