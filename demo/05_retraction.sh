#!/usr/bin/env bash
# 演示 5：撤回误收报告 -> 同键多行原子删除并登记拒收 -> 一切补录/改键被拒 -> 同键重试幂等。
set -euo pipefail
cd "$(dirname "$0")"
source ./common.sh

echo "== 1. 准备数据：同键重复 3 行（模拟唯一索引未生效）+ 另一实验室同号 1 行 =="
psql_exec -c "TRUNCATE reports RESTART IDENTITY;" \
          -c "TRUNCATE report_rejections RESTART IDENTITY;" >/dev/null
psql_exec -c "
INSERT INTO reports(lab_no, report_no, measured) VALUES
 ('LAB-001','RPT-1',10),('LAB-001','RPT-1',11),('LAB-001','RPT-1',12),
 ('LAB-002','RPT-1',20);"
psql_exec -c "SELECT lab_no, report_no, count(*) FROM reports GROUP BY 1,2 ORDER BY 1,2;"

echo "== 2. 首次撤回：预期 201，deleted_rows=3 =="
c -X POST "$API_BASE/reports/retract" -d '{"lab_no":"LAB-001","report_no":"RPT-1"}'

echo "== 3. 核对：该键 reports 零行；拒收登记存在；LAB-002 同号不受影响 =="
psql_exec -c "SELECT lab_no, report_no, count(*) AS reports_left
              FROM reports GROUP BY 1,2 ORDER BY 1,2;"
psql_exec -c "SELECT lab_no, report_no, reason, deleted_rows FROM report_rejections;"

echo "== 4. 同键重试：预期 200 且 retried=true，登记不新增 =="
c -X POST "$API_BASE/reports/retract" -d '{"lab_no":"LAB-001","report_no":"RPT-1"}'

echo "== 5. 撤回后 API 补录被拒（409 report_retracted） =="
c -X POST "$API_BASE/reports" -d '{"lab_no":"LAB-001","report_no":"RPT-1","measured":99}' || true

echo "== 6. 独立 SQL 补录 / UPDATE 改键到拒收键同样被触发器拒绝（SQLSTATE R9001） =="
psql_exec -c "INSERT INTO reports(lab_no,report_no,measured) VALUES ('LAB-001','RPT-1',99);" || true
psql_exec -c "UPDATE reports SET lab_no='LAB-001', report_no='RPT-1' WHERE lab_no='LAB-002';" || true
psql_exec -c "SELECT lab_no, report_no, count(*) AS rows_left FROM reports GROUP BY 1,2 ORDER BY 1,2;"

echo "== 7. 其他实验室同号报告仍可正常补录（用新报告号，避免人为制造重复键） =="
c -X POST "$API_BASE/reports" -d '{"lab_no":"LAB-002","report_no":"RPT-2","measured":21}'

echo "== 8. 在线建唯一索引与正常补录不受影响 =="
psql_exec -c "DROP INDEX IF EXISTS uq_reports_lab_no_report_no;" \
          -c "DELETE FROM index_build_rounds;" >/dev/null
c -X POST "$API_BASE/index/build" >/dev/null
wait_status complete 30
c "$API_BASE/index/status" | python3 -c 'import sys,json;s=json.load(sys.stdin);print("index valid:",s["index"]["is_valid"])'
echo "DEMO-5 OK"
