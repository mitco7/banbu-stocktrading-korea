-- decision_logs: 일자별 최종 매매 의사결정 로그 (Step 5 산출물).
-- 종합점수 + 하드필터 + VIX 게이트 + LLM 거부권 검토를 거친 종목별 최종 결정(BUY/HOLD)과 근거.
-- (decision_date, ticker) 복합키 upsert — 같은 날 재실행은 갱신(멱등), 다른 날짜는 이력 보존.
-- ⚠ fail-close 기본: LLM 실패/키없음/파싱실패/타임아웃 어떤 경우든 BUY 아님(매수 차단). decision=HOLD.
-- ⚠ LLM 은 거부권만 — 후보에 없던 종목 추가/탈락 종목 되살리기 불가(코드에서도 차단).

CREATE TABLE IF NOT EXISTS decision_logs (
    decision_date DATE NOT NULL,         -- 결정 산출일(KST 영업일)
    ticker TEXT NOT NULL,                -- 종목코드 6자리
    name TEXT,                           -- 종목명

    decision TEXT,                       -- 'BUY' / 'HOLD' (BUY 는 모든 게이트 통과 + LLM BUY 일 때만)
    reason TEXT,                         -- LLM 사유 또는 탈락 사유
    stage TEXT,                          -- 'llm_reviewed' / 'hard_filtered' / 'market_blocked'

    -- ── 종합점수 / 서브스코어(통과 종목만 채움) ──
    total_score FLOAT8,
    ml_sub FLOAT8,
    tech_sub FLOAT8,
    flow_sub FLOAT8,
    vol_sub FLOAT8,
    dual_buy BOOLEAN,                    -- 외국인·기관 동반 순매수(LLM 근거용 플래그)

    -- ── 컨텍스트 ──
    predicted_rise_pct FLOAT8,           -- ML 예측 상승률(%, 확률 아님)
    flow_score FLOAT8,
    rank INT,                            -- 그날 통과 종목 내 정렬 순위(1=최상위)
    vix_t1 FLOAT8,                       -- 게이트에 쓴 직전영업일 VIX
    market_regime TEXT,                  -- 'normal' / 'caution' / 'blocked'
    market_analysis TEXT,                -- LLM 시장 총평(그날 공통)
    llm_model TEXT,                      -- 최종 결정에 쓰인 모델(폴백 결과). 실패 시 NULL
    llm_ok BOOLEAN,                      -- LLM 정상 응답 여부(False=fail-close 차단)

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (decision_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_decision_logs_date ON decision_logs (decision_date);

-- updated_at 자동 갱신(007 에서 만든 함수 재사용).
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_decision_logs_updated_at ON decision_logs;
CREATE TRIGGER trg_decision_logs_updated_at
    BEFORE UPDATE ON decision_logs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE decision_logs DISABLE ROW LEVEL SECURITY;
