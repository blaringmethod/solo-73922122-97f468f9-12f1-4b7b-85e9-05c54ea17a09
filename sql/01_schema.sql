-- 检验数仓报告表与索引构建轮次表
-- 幂等脚本，容器首次启动时由 PostgreSQL 自动执行（/docker-entrypoint-initdb.d）。

BEGIN;

-- 1) 报告主表
CREATE TABLE IF NOT EXISTS reports (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- 主键流水号
    lab_no       TEXT NOT NULL,                                     -- 实验室号（非空）
    report_no    TEXT NOT NULL,                                     -- 实验室内报告号（非空）
    measured     INTEGER NOT NULL,                                  -- 整数测值
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  reports            IS '持续补录的检验报告表';
COMMENT ON COLUMN reports.id         IS '主键流水号';
COMMENT ON COLUMN reports.lab_no     IS '实验室号';
COMMENT ON COLUMN reports.report_no  IS '实验室内报告号';
COMMENT ON COLUMN reports.measured   IS '整数测值';

-- 补录期间的普通辅助索引（非唯一），仅用于按实验室号/报告号查询与去重；
-- 唯一索引构建成功前，唯一性由补录 SQL 自行保证。
CREATE INDEX IF NOT EXISTS idx_reports_lab_report
    ON reports (lab_no, report_no);

-- 2) 报告撤回拒收登记表
-- 实验室撤回误收报告后登记 (lab_no, report_no)：该键下 reports 全部行被原子删除，
-- 此后任何路径（API 补录、独立 SQL、UPDATE 改键到该键）均由触发器拒绝。
-- 不同实验室即使报告号相同也互不影响（唯一键含 lab_no）。
CREATE TABLE IF NOT EXISTS report_rejections (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lab_no       TEXT NOT NULL,                                     -- 实验室号（非空）
    report_no    TEXT NOT NULL,                                     -- 实验室内报告号（非空）
    reason       TEXT NOT NULL DEFAULT 'lab_retracted',             -- 拒收原因
    deleted_rows INTEGER NOT NULL DEFAULT 0,                        -- 撤回时删除的 reports 行数
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),                -- 首次撤回时间
    CONSTRAINT uq_report_rejections_key UNIQUE (lab_no, report_no)
);

COMMENT ON TABLE  report_rejections             IS '撤回误收报告的拒收登记，键为实验室号+报告号';
COMMENT ON COLUMN report_rejections.deleted_rows IS '撤回事务中从 reports 删除的同键行数（含唯一索引未生效时的重复行）';

-- 撤回与补录共用同一把事务级咨询锁（对键文本哈希取 64bit），保证二者严格串行、
-- 不出现死锁：成功撤回提交后，同键补录一定能读到拒收登记并被拒绝。
-- 抽成 SQL 函数，使触发器与撤回事务使用完全相同的加键方式，避免两处漂移。
CREATE OR REPLACE FUNCTION report_key_advisory_lock(p_lab_no TEXT, p_report_no TEXT)
RETURNS void
LANGUAGE sql AS $$
    SELECT pg_advisory_xact_lock(
        hashtextextended(format('%s|%s', p_lab_no, p_report_no), 0)
    );
$$;

CREATE OR REPLACE FUNCTION report_rejection_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
DECLARE
    v_lab_no    TEXT;
    v_report_no TEXT;
    v_rejected  BOOLEAN;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- 仅当键发生变化时才检查（不改键的普通更新直接放行）。
        IF NEW.lab_no = OLD.lab_no AND NEW.report_no = OLD.report_no THEN
            RETURN NEW;
        END IF;
    END IF;
    v_lab_no    := NEW.lab_no;
    v_report_no := NEW.report_no;

    -- 与撤回事务同一把锁：撤回未提交时补录在锁上等待，提交后必见拒收登记。
    PERFORM report_key_advisory_lock(v_lab_no, v_report_no);

    SELECT TRUE INTO v_rejected
    FROM report_rejections
    WHERE lab_no = v_lab_no AND report_no = v_report_no;

    IF v_rejected THEN
        RAISE EXCEPTION
            'REJECTED_KEY: report (lab_no=%, report_no=%) has been retracted and is blocklisted',
            v_lab_no, v_report_no
            USING ERRCODE = 'R9001';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION report_rejection_guard() IS
    'reports 行级守卫：命中 report_rejections 拒收键的 INSERT/改键 UPDATE 一律拒绝（SQLSTATE R9001）';

DROP TRIGGER IF EXISTS trg_reports_rejection_guard ON reports;
CREATE TRIGGER trg_reports_rejection_guard
    BEFORE INSERT OR UPDATE ON reports
    FOR EACH ROW
    EXECUTE FUNCTION report_rejection_guard();

-- 3) 唯一索引构建轮次表（状态持久化，API 重启后据此接续跟踪）
CREATE TABLE IF NOT EXISTS index_build_rounds (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_id    UUID NOT NULL UNIQUE,
    status      TEXT NOT NULL CHECK (status IN (
                    'running',        -- 构建中（含等待长事务阶段）
                    'complete',       -- 索引已有效
                    'failed',         -- 构建失败（如重复键），原数据保留，无效索引已清理
                    'cancel_requested', -- 已受理取消，等待后端 SQL 真正停止
                    'cancelled'       -- 后端 SQL 已确认停止且索引无效
                )),
    backend_pid INTEGER,                       -- 执行 CREATE INDEX 的 PostgreSQL 后端 PID
    index_name  TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    error       TEXT,                         -- 失败原因（如重复键明细）
    failure_type TEXT,                        -- duplicate_key / lock_timeout / other / cancelled
    restarted   BOOLEAN NOT NULL DEFAULT FALSE -- 是否为 API 重启后接续跟踪的轮次
);

COMMENT ON TABLE index_build_rounds IS 'CREATE UNIQUE INDEX CONCURRENTLY 构建轮次';

-- 同一时刻只允许一轮活跃构建（running 或 cancel_requested 各最多一条）。
-- 该约束在数据库层面兜底，即使多个 API 实例并发发起也不会出现两轮并发构建。
CREATE UNIQUE INDEX IF NOT EXISTS uq_index_rounds_one_active
    ON index_build_rounds ((1))
    WHERE status IN ('running', 'cancel_requested');

COMMIT;
