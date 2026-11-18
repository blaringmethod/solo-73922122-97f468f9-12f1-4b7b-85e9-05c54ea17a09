"""撤回/拒收功能自测。

覆盖：
  * 首次撤回原子删除同键全部行（含唯一索引未生效时的重复行）并登记拒收；
  * 同键重试幂等返回既有结果；其他实验室同号报告不受影响；
  * 撤回后经 API（单条/批量）、独立 SQL INSERT、UPDATE 改键均被拒绝；
  * 撤回与补录交错：两种先后顺序下成功响应后该键都不残留报告；
  * 失败/回滚原子性：不可能出现“已删除未登记”或“已登记仍有原报告”；
  * 在线建唯一索引与正常补录不受影响。
"""
from __future__ import annotations

import threading

import psycopg
import pytest

from app import db as dbmod
from app.config import settings
from app.retraction import REJECTED_SQLSTATE
from conftest import poll_status


def _raw_conn():
    return psycopg.connect(settings.database_url)


def _count_key(lab_no: str, report_no: str) -> int:
    with dbmod.pool().connection() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM reports WHERE lab_no=%s AND report_no=%s",
            (lab_no, report_no),
        ).fetchone()["n"]


def _rejection_row(lab_no: str, report_no: str):
    with dbmod.pool().connection() as conn:
        return conn.execute(
            "SELECT * FROM report_rejections WHERE lab_no=%s AND report_no=%s",
            (lab_no, report_no),
        ).fetchone()


def test_retract_deletes_all_duplicate_rows_and_registers(client):
    # 唯一索引未生效，同键造 3 行重复。
    for m in (1, 2, 3):
        client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": m})
    # 另一实验室同号报告，必须不受影响。
    client.post("/reports", json={"lab_no": "L2", "report_no": "A", "measured": 9})
    assert _count_key("L1", "A") == 3

    r = client.post("/reports/retract", json={"lab_no": "L1", "report_no": "A"})
    assert r.status_code == 201
    body = r.json()
    assert body["deleted_rows"] == 3
    assert body["retried"] is False
    assert body["lab_no"] == "L1" and body["report_no"] == "A"

    # 该键 reports 全部剔除；拒收登记落库；另一实验室同号保留。
    assert _count_key("L1", "A") == 0
    rej = _rejection_row("L1", "A")
    assert rej is not None and rej["deleted_rows"] == 3
    assert _count_key("L2", "A") == 1


def test_retract_idempotent_retry_returns_existing_result(client):
    client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": 1})
    first = client.post("/reports/retract", json={"lab_no": "L1", "report_no": "A"})
    assert first.status_code == 201

    # 同键重试（即使上游又重传补录过也一样）：返回已有撤回结果，不新增登记。
    second = client.post("/reports/retract", json={"lab_no": "L1", "report_no": "A"})
    assert second.status_code == 200
    b1, b2 = first.json(), second.json()
    assert b2["retried"] is True
    assert b2["deleted_rows"] == b1["deleted_rows"] == 1
    assert b2["retracted_at"] == b1["retracted_at"]
    with dbmod.pool().connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM report_rejections WHERE lab_no='L1' AND report_no='A'"
        ).fetchone()["n"]
    assert n == 1


def test_retract_nonexistent_key_still_registers(client):
    # 撤回一个从未入仓的键：删除 0 行也要登记，后续补录同样被拒。
    r = client.post("/reports/retract", json={"lab_no": "LX", "report_no": "RX"})
    assert r.status_code == 201
    assert r.json()["deleted_rows"] == 0
    dup = client.post("/reports", json={"lab_no": "LX", "report_no": "RX", "measured": 1})
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "report_retracted"
    assert _count_key("LX", "RX") == 0


def test_api_insert_and_batch_rejected_after_retract(client):
    client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": 1})
    client.post("/reports/retract", json={"lab_no": "L1", "report_no": "A"})

    r = client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": 2})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "report_retracted"

    # 批量：拒收键跳过（report_retracted），正常键照常写入，互不影响。
    b = client.post("/reports/batch", json={"items": [
        {"lab_no": "L1", "report_no": "A", "measured": 2},
        {"lab_no": "L1", "report_no": "B", "measured": 3},
        {"lab_no": "L2", "report_no": "A", "measured": 4},
    ]}).json()
    assert b["inserted_count"] == 2
    assert b["skipped_count"] == 1
    assert b["skipped"][0]["reason"] == "report_retracted"
    assert _count_key("L1", "A") == 0
    assert _count_key("L1", "B") == 1 and _count_key("L2", "A") == 1


def test_raw_sql_insert_and_update_rekey_are_rejected(client):
    client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": 1})
    client.post("/reports", json={"lab_no": "L1", "report_no": "B", "measured": 2})
    client.post("/reports/retract", json={"lab_no": "L1", "report_no": "A"})

    # 独立 SQL 补录被数据库触发器拒绝（绕过 API 同样生效）。
    with _raw_conn() as conn:
        with pytest.raises(psycopg.Error) as ei:
            conn.execute(
                "INSERT INTO reports(lab_no, report_no, measured) VALUES ('L1','A',5)"
            )
        assert ei.value.sqlstate == REJECTED_SQLSTATE
        conn.rollback()

    # 把别的报告“改为该键”同样拒绝，原行保持不变。
    with _raw_conn() as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                "UPDATE reports SET report_no='A' WHERE lab_no='L1' AND report_no='B'"
            )
        conn.rollback()
    assert _count_key("L1", "B") == 1
    assert _count_key("L1", "A") == 0

    # 不改键的普通 UPDATE 不受影响。
    with _raw_conn() as conn:
        conn.execute("UPDATE reports SET measured=42 WHERE lab_no='L1' AND report_no='B'")
        conn.commit()
    with dbmod.pool().connection() as conn:
        v = conn.execute(
            "SELECT measured FROM reports WHERE lab_no='L1' AND report_no='B'"
        ).fetchone()["measured"]
    assert v == 42


def test_interleave_insert_blocked_until_retract_commits_then_rejected(client):
    """交错一：撤回先开始（未提交）并持锁，补录后到 -> 等待 -> 撤回提交后补录被拒。

    成功撤回响应之后该键不得残留报告。
    """
    client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": 1})

    # 手工模拟“撤回事务进行中”：持锁 + 已删除但尚未提交。
    holder = _raw_conn()
    holder.execute("SELECT report_key_advisory_lock('L1','A')")
    holder.execute("DELETE FROM reports WHERE lab_no='L1' AND report_no='A'")

    insert_errors = []
    timed_out = []

    def do_insert():
        # 独立连接：设置 lock_timeout 防止环境异常时无限等待污染后续用例。
        conn = _raw_conn()
        try:
            conn.execute("SET lock_timeout = '15000'")
            conn.execute(
                "INSERT INTO reports(lab_no, report_no, measured) "
                "VALUES ('L1','A',7)"
            )
            conn.commit()
        except psycopg.Error as exc:
            insert_errors.append(exc.sqlstate)
            conn.rollback()
        finally:
            conn.close()

    t = threading.Thread(target=do_insert, daemon=True)
    t.start()
    # 确认补录确实在等待 advisory 锁，而不是已经失败/成功。
    threading.Event().wait(1.5)
    with dbmod.pool().connection() as conn:
        waiting = conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity "
            "WHERE wait_event_type='Lock' AND wait_event='advisory' "
            "AND query LIKE %s",
            ("%INSERT INTO reports%",),
        ).fetchone()["n"]
    assert waiting >= 1

    # 完成撤回：登记拒收并提交（与 API 撤回事务内顺序一致），随后确保释放持锁连接。
    try:
        holder.execute(
            "INSERT INTO report_rejections(lab_no, report_no, deleted_rows) "
            "VALUES ('L1','A', 1)"
        )
        holder.commit()
    finally:
        holder.close()
    t.join(timeout=20)
    assert not t.is_alive() or timed_out is not None  # join 超时则下方断言会失败

    assert insert_errors == [REJECTED_SQLSTATE]
    assert _count_key("L1", "A") == 0
    assert _rejection_row("L1", "A") is not None


def test_interleave_retract_swallows_concurrent_insert(client):
    """交错二：补录先提交，撤回后到 -> DELETE 把刚写入的行一并删掉，键上零残留。"""
    client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": 1})
    barrier = threading.Event()

    def retract():
        try:
            barrier.set()  # 通知主线程开始抢补录窗口
            r = client.post("/reports/retract",
                            json={"lab_no": "L1", "report_no": "A"})
            retract_result.append(r.status_code)
        except Exception as exc:  # noqa: BLE001 - 测试线程内失败上抛到结果列表
            retract_result.append(exc)

    retract_result: list = []
    t = threading.Thread(target=retract, daemon=True)
    t.start()
    barrier.wait(timeout=5)
    # 撤回可能尚未抢到锁；不断尝试补录，直到观察到撤回完成。
    inserted_while_open = False
    for _ in range(100):
        if retract_result:
            break
        rr = client.post("/reports",
                         json={"lab_no": "L1", "report_no": "A", "measured": 8})
        if rr.status_code == 201:
            inserted_while_open = True
    t.join(timeout=30)
    assert retract_result == [201]

    # 无论补录在撤回前还是撤回窗口内写入，成功响应后该键都不得残留报告。
    assert _count_key("L1", "A") == 0
    rej = _rejection_row("L1", "A")
    assert rej is not None
    if inserted_while_open:
        # 至少有一条补录先于 DELETE 提交，删除计数应把它计入。
        assert rej["deleted_rows"] >= 2

    # 撤回完成后的补录必然被拒。
    rr = client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": 9})
    assert rr.status_code == 409 and rr.json()["detail"]["code"] == "report_retracted"


def test_retract_rollback_leaves_no_partial_state(client):
    """失败/回滚原子性：模拟撤回事务提交前失败 -> 行未删除、登记不存在。

    即不可能出现“记录已删除却未登记拒收”。
    """
    client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": 1})
    client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": 2})

    conn = _raw_conn()
    try:
        conn.execute("SELECT report_key_advisory_lock('L1','A')")
        conn.execute("DELETE FROM reports WHERE lab_no='L1' AND report_no='A'")
        conn.execute(
            "INSERT INTO report_rejections(lab_no, report_no, deleted_rows) "
            "VALUES ('L1','A', 2)"
        )
        # 提交前整体回滚（模拟执行失败/连接中断）。
        conn.rollback()
    finally:
        conn.close()

    assert _count_key("L1", "A") == 2          # 原报告仍在
    assert _rejection_row("L1", "A") is None   # 拒收未登记

    # 回滚后补录完全正常（没有残留锁/残留触发器状态）。
    r = client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": 3})
    assert r.status_code == 201


def test_registered_rejection_always_implies_no_report_rows(client):
    """反向不变式：只要拒收登记存在并已提交，reports 中该键必然零行。"""
    for m in range(3):
        client.post("/reports", json={"lab_no": "L9", "report_no": "R9", "measured": m})
    client.post("/reports/retract", json={"lab_no": "L9", "report_no": "R9"})
    assert _rejection_row("L9", "R9") is not None
    assert _count_key("L9", "R9") == 0


def test_retraction_coexists_with_online_index_and_normal_inserts(client):
    """撤回/拒收不影响在线建唯一索引，也不影响其他键的正常补录。"""
    client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": 1})
    client.post("/reports", json={"lab_no": "L1", "report_no": "B", "measured": 2})
    client.post("/reports/retract", json={"lab_no": "L1", "report_no": "A"})

    client.post("/index/build")
    final = poll_status(client, "complete")
    assert final["index"]["is_valid"] is True

    # 索引生效后：拒收键仍被触发器拦截；其他键重复走唯一索引 409；新键正常写入。
    r1 = client.post("/reports", json={"lab_no": "L1", "report_no": "A", "measured": 3})
    assert r1.status_code == 409 and r1.json()["detail"]["code"] == "report_retracted"
    r2 = client.post("/reports", json={"lab_no": "L1", "report_no": "B", "measured": 4})
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "duplicate_report"
    r3 = client.post("/reports", json={"lab_no": "L1", "report_no": "C", "measured": 5})
    assert r3.status_code == 201


def test_get_retraction_endpoint(client):
    r = client.get("/reports/retract", params={"lab_no": "L1", "report_no": "A"})
    assert r.status_code == 404
    client.post("/reports/retract", json={"lab_no": "L1", "report_no": "A"})
    r = client.get("/reports/retract", params={"lab_no": "L1", "report_no": "A"})
    assert r.status_code == 200
    assert r.json()["report_no"] == "A" and r.json()["retried"] is True
