"""FastAPI 入口：补录、唯一索引构建的发起/查询/取消。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException, status

from . import db
from .responses import AppJSONResponse
from .retraction import is_rejected_key_error
from . import retraction as retraction_service
from .schemas import BatchReportIn, ReportIn, ReportOut, RetractIn
from .service import service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_pool()
    service.reconcile_on_startup()
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(
    title="检验报告唯一性保障 API",
    description=(
        "对持续补录的 reports 表在线构建 (lab_no, report_no) 唯一索引；"
        "支持发起、查询、取消 CREATE UNIQUE INDEX CONCURRENTLY，"
        "构建期间补录不阻塞，API 重启后接续跟踪。"
    ),
    version="1.0.0",
    default_response_class=AppJSONResponse,
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    with db.pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


# ------------------------------ 补录 ------------------------------------

def _rejected_http_exception(payload: ReportIn, exc: psycopg.Error) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "report_retracted",
            "message": "该实验室下此报告号已被撤回并登记拒收，补录被拒绝",
            "lab_no": payload.lab_no,
            "report_no": payload.report_no,
            "pg_error": str(exc).strip(),
        },
    )


@app.post("/reports", response_model=ReportOut, status_code=201)
def insert_report(payload: ReportIn):
    """单条补录。构建期间（CONCURRENTLY）可正常提交；唯一索引生效后重复键返回 409；
    命中撤回拒收键（含独立 SQL / UPDATE 改键，由数据库触发器兜底）返回 409。"""
    try:
        with db.pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO reports (lab_no, report_no, measured) "
                "VALUES (%s, %s, %s) RETURNING id, lab_no, report_no, measured",
                (payload.lab_no, payload.report_no, payload.measured),
            ).fetchone()
            conn.commit()
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_report",
                "message": "同一实验室下报告号已存在，本条补录被拒绝，原有数据保持不变",
                "lab_no": payload.lab_no,
                "report_no": payload.report_no,
                "pg_error": str(exc).strip(),
            },
        )
    except psycopg.Error as exc:
        if is_rejected_key_error(exc):
            raise _rejected_http_exception(payload, exc)
        raise
    return dict(row)


@app.post("/reports/batch")
def insert_reports_batch(payload: BatchReportIn):
    """批量补录：逐条保存，重复键/撤回键跳过且不影响其它行，返回成功/跳过明细。"""
    inserted, skipped = [], []
    with db.pool().connection() as conn:
        for item in payload.items:
            # 每条用保存点隔离：单行失败只回滚该行，前面已成功的行不被连带撤销。
            conn.execute("SAVEPOINT item")
            try:
                row = conn.execute(
                    "INSERT INTO reports (lab_no, report_no, measured) "
                    "VALUES (%s, %s, %s) RETURNING id, lab_no, report_no, measured",
                    (item.lab_no, item.report_no, item.measured),
                ).fetchone()
                inserted.append(dict(row))
            except psycopg.errors.UniqueViolation:
                conn.execute("ROLLBACK TO SAVEPOINT item")
                skipped.append(
                    {"lab_no": item.lab_no, "report_no": item.report_no,
                     "reason": "duplicate_report"}
                )
            except psycopg.Error as exc:
                if is_rejected_key_error(exc):
                    conn.execute("ROLLBACK TO SAVEPOINT item")
                    skipped.append(
                        {"lab_no": item.lab_no, "report_no": item.report_no,
                         "reason": "report_retracted"}
                    )
                else:
                    raise
        conn.commit()
    return {"inserted_count": len(inserted), "skipped_count": len(skipped),
            "inserted": inserted, "skipped": skipped}


@app.post("/reports/retract", status_code=201)
def retract_report(payload: RetractIn):
    """撤回误收报告：单事务原子删除该 (lab_no, report_no) 下全部 reports 行
    （唯一索引未生效时的同号多行也全部剔除）并登记拒收。

    * 首次撤回返回 201；同键重试返回 200 与已有撤回结果（retried=true）；
    * 撤回后任何补录或把别的报告改为该键都被数据库触发器拒绝（API 与独立 SQL 同效）；
    * 其他实验室的同号报告不受影响。
    """
    view, code = retraction_service.retract_report(payload.lab_no, payload.report_no)
    return AppJSONResponse(view, status_code=code)


@app.get("/reports/retract")
def get_retraction(lab_no: str, report_no: str):
    """查询某键的拒收登记；未登记返回 404。"""
    view = retraction_service.get_rejection(lab_no, report_no)
    if view is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "retraction_not_found",
                    "lab_no": lab_no, "report_no": report_no},
        )
    return view


@app.get("/reports")
def list_reports(limit: int = 50, offset: int = 0):
    with db.pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, lab_no, report_no, measured FROM reports "
            "ORDER BY id DESC LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT count(*) AS n FROM reports").fetchone()["n"]
    return {"total": total, "items": [dict(r) for r in rows]}


# --------------------------- 唯一索引构建 --------------------------------

@app.post("/index/build", status_code=202)
def start_build():
    """发起（或复用）CREATE UNIQUE INDEX CONCURRENTLY。

    * 已有活跃轮次：复用同一轮，不新开；
    * 索引已有效：直接返回 complete；
    * 失败后的无效残留索引会先安全清理，绝不删除有效索引。
    """
    view, code = service.start_build()
    return AppJSONResponse(view, status_code=code)


@app.get("/index/status")
def index_status(round_id: str | None = None):
    """查询构建轮次状态，并附带 pg_index 中的真实索引有效性。"""
    return service.get_status(round_id)


@app.post("/index/cancel", status_code=202)
def cancel_build(round_id: str | None = None):
    """取消构建：先受理（cancel_requested），后端 SQL 确认停止后才转 cancelled。

    只向记录在案的构建后端 PID 发 pg_cancel_backend，不影响补录连接；
    若索引已有效则报 complete。
    """
    view, code = service.request_cancel(round_id)
    return AppJSONResponse(view, status_code=code)
