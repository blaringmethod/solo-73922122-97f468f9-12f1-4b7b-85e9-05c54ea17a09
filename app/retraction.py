"""报告撤回服务：原子删除同键全部 reports 行并登记拒收。

核心不变式（全部在单个数据库事务内完成）：
    拒收登记与 reports 删除同生共死——事务提交则两者同时生效，回滚则两者皆无；
    不可能出现“行已删除却未登记拒收”或“已登记却仍有原报告”的中间状态。

串行化：撤回事务与补录触发器对同一 (lab_no, report_no) 使用同一把事务级
advisory 锁（见 sql/01_schema.sql 的 report_key_advisory_lock）：
  * 撤回先到：补录在锁上等待，撤回提交后补录读到拒收登记 -> 拒绝；
  * 补录先到：撤回等锁，补录提交后 DELETE 会把刚写入的行一并删掉。
因此成功响应之后，该键不可能再残留任何报告。
"""
from __future__ import annotations

import logging

import psycopg

from . import db

log = logging.getLogger("retraction")

# 与触发器 report_rejection_guard 中 RAISE EXCEPTION USING ERRCODE 保持一致。
REJECTED_SQLSTATE = "R9001"


def is_rejected_key_error(exc: psycopg.Error) -> bool:
    """识别“命中拒收键”错误（SQLSTATE R9001）。"""
    return isinstance(exc, psycopg.Error) and exc.sqlstate == REJECTED_SQLSTATE


def _rejection_view(row: dict, *, retried: bool) -> dict:
    return {
        "lab_no": row["lab_no"],
        "report_no": row["report_no"],
        "reason": row["reason"],
        "deleted_rows": row["deleted_rows"],
        "retracted_at": row["created_at"].isoformat(),
        "retried": retried,
    }


def retract_report(lab_no: str, report_no: str) -> tuple[dict, int]:
    """撤回一个 (lab_no, report_no) 键。

    返回 (响应视图, HTTP 状态码)：
      * 首次撤回：201，删除该键全部行（唯一索引未生效时的重复行也一并剔除）并登记；
      * 同键重试：200，原样返回已有撤回结果，不重复删除/登记。
    """
    with db.pool().connection() as conn:
        # 1) 先抢该键的事务级咨询锁：与补录路径严格串行，杜绝撤回/补录交错漏网。
        conn.execute(
            "SELECT report_key_advisory_lock(%s, %s)",
            (lab_no, report_no),
        )

        # 2) 已有拒收登记：同键重试，返回既有结果（仍在同一把锁的保护下读取）。
        existing = conn.execute(
            "SELECT lab_no, report_no, reason, deleted_rows, created_at "
            "FROM report_rejections WHERE lab_no = %s AND report_no = %s",
            (lab_no, report_no),
        ).fetchone()
        if existing is not None:
            conn.rollback()  # 只读，显式结束事务、释放锁
            log.info("retract retry for (%s,%s); returning existing record",
                     lab_no, report_no)
            return _rejection_view(dict(existing), retried=True), 200

        # 3) 首次撤回：删除该键下全部 reports 行，再登记拒收。
        #    BEFORE 行触发器只作用于 reports 的 INSERT/UPDATE，DELETE 不会被拦截。
        #    唯一索引未生效时的同号多行由单条 DELETE 全部剔除。
        deleted = conn.execute(
            "DELETE FROM reports WHERE lab_no = %s AND report_no = %s",
            (lab_no, report_no),
        ).rowcount
        row = conn.execute(
            "INSERT INTO report_rejections (lab_no, report_no, reason, deleted_rows) "
            "VALUES (%s, %s, 'lab_retracted', %s) "
            "RETURNING lab_no, report_no, reason, deleted_rows, created_at",
            (lab_no, report_no, deleted),
        ).fetchone()
        conn.commit()  # 删除与登记原子提交；任何失败整体回滚，不变式不破。

    log.info("retracted (%s,%s): deleted %s report row(s)", lab_no, report_no, deleted)
    return _rejection_view(dict(row), retried=False), 201


def get_rejection(lab_no: str, report_no: str) -> dict | None:
    """查询拒收登记；不存在返回 None。"""
    with db.pool().connection() as conn:
        row = conn.execute(
            "SELECT lab_no, report_no, reason, deleted_rows, created_at "
            "FROM report_rejections WHERE lab_no = %s AND report_no = %s",
            (lab_no, report_no),
        ).fetchone()
    return _rejection_view(dict(row), retried=True) if row else None
