-- pipeline_runs: 스케줄 잡/파이프라인 실행 이력 (Step 7 — 무인 운영 관측).
-- "어제 몇 시에 뭐가 어디까지 돌았고 왜 멈췄나"를 로그 grep 없이 재구성하기 위한 테이블.
-- 기록 실패는 잡을 멈추지 않는다(코드에서 try/except 로 삼킴 — 관측 수단이지 제어 흐름 아님).
--
-- ⚠ Supabase SQL Editor 에서 수동 실행(기존 001~011 과 동일한 방식).

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id BIGSERIAL PRIMARY KEY,
    job TEXT NOT NULL,                   -- daily_pipeline / buy / sell_cycle / reconcile ...
    run_date DATE NOT NULL,              -- KST 기준 실행일
    status TEXT NOT NULL,                -- running / ok / failed
    error TEXT,                          -- 실패 사유(성공 시 NULL)
    result JSONB,                        -- 요약 결과(단계별 상태 등)
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

-- 일자·잡별 조회(운영 대시보드/디버깅).
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_job_date
    ON pipeline_runs (job, run_date DESC);

ALTER TABLE pipeline_runs DISABLE ROW LEVEL SECURITY;
