-- stock_recommendations: 일자별 **유니버스 25종목 전체** 분석 스냅샷 + 매수 후보 표시 (Step 4 산출물).
-- 후보 여부와 무관하게 25종목 모두 저장한다(is_candidate 로 구분). 다음 스텝(종합점수·LLM)이 전 종목
--   데이터를 활용하면서도 후보만 필터할 수 있게 함.
-- is_candidate = ML 예측 상승률(predicted_rise_pct = (예측가-현재가)/현재가×100; 확률 아님·CLAUDE.md §2.2)
--   이 RECOMMEND_MIN_RISE_PCT(기본 3%) 이상이면 TRUE(매수 후보). 각 종목에 기술적 지표 + 거래량비율 + 수급점수 결합.
-- (날짜, ticker) 복합키로 **일자별 이력 보존**(다른 날짜는 그대로). 같은 날 재실행은
--   그 날짜 행을 전부 삭제 후 재삽입해 통째 갱신(멱등, predict.py 패턴 — 고아 행 방지).
--   "그날 종목별 지표/후보 여부가 어땠는지" 사후 추적용. 종합점수 가중합·LLM 검토는 다음 스텝(여기선 미포함).
-- ⚠ 지표는 FDR 일봉 OHLCV(거래일만)로 계산 — market_data 는 종가만/주말 ffill 이라 ATR/ADX(고저가 필요)
--    계산 불가하고 비거래일 잡음이 있음. indicator_date = 계산에 쓰인 마지막 거래일.

CREATE TABLE IF NOT EXISTS stock_recommendations (
    "날짜" DATE NOT NULL,                 -- 추천 산출일(KST 영업일). 일자별 추적의 1차 키.
    ticker TEXT NOT NULL,                -- 종목코드 6자리
    name TEXT,                           -- 종목명

    -- ── ML 예측 근거(stock_analysis_results) ──
    last_actual_price FLOAT8,            -- 최근 실제가
    predicted_future_price FLOAT8,       -- 14일 후 예측가
    predicted_rise_pct FLOAT8,           -- 예측 상승률(%) = (예측가-현재가)/현재가×100 (확률 아님)
    predicted_rise BOOLEAN,              -- 상승 예측 여부(예측가 > 현재가)
    ml_recommendation TEXT,              -- ML 라벨(STRONG BUY/BUY 등, 참고용)
    is_candidate BOOLEAN,                -- 매수 후보 여부(predicted_rise_pct >= rise_threshold)

    -- ── 기술적 지표 값(판단 근거 — 최대한 많이) ──
    close_price FLOAT8,                  -- 지표 기준 최근 종가
    sma_short FLOAT8,                    -- 단기 이동평균(기본 20)
    sma_long FLOAT8,                     -- 장기 이동평균(기본 50)
    golden_cross BOOLEAN,                -- 골든크로스(단기 > 장기)
    rsi FLOAT8,                          -- RSI(Wilder, 기본 14)
    rsi_signal TEXT,                     -- 'overbought'/'oversold'/'neutral'
    macd FLOAT8,                         -- MACD line
    macd_signal FLOAT8,                  -- Signal line
    macd_hist FLOAT8,                    -- MACD - Signal
    macd_buy BOOLEAN,                    -- 매수신호(MACD > Signal)
    atr FLOAT8,                          -- ATR(Wilder, 기본 14) — 익절/손절은 주문 단계에서 사용
    atr_pct FLOAT8,                      -- ATR/종가×100 (변동성 %)
    adx FLOAT8,                          -- ADX(Wilder, 기본 14) — 추세 강도
    plus_di FLOAT8,                      -- +DI
    minus_di FLOAT8,                     -- -DI
    trend_strong BOOLEAN,               -- ADX >= ADX_TREND_MIN(기본 25)
    volume_ratio FLOAT8,                 -- 오늘 거래량 / 직전 N거래일 평균

    -- ── 수급 근거(investor_flow) ──
    flow_score FLOAT8,                   -- -1~+1 (양수=매수우위). 감성점수 자리
    foreign_net FLOAT8,                  -- 외국인 순매수 합(원)
    institution_net FLOAT8,              -- 기관 순매수 합(원)
    flow_calc_date DATE,                 -- 수급 점수 기준일

    -- ── 메타(재현성) ──
    indicator_date DATE,                 -- 지표 계산에 쓰인 마지막 거래일
    rise_threshold FLOAT8,               -- 적용된 1차 필터 임계값(%)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY ("날짜", ticker)
);

CREATE INDEX IF NOT EXISTS idx_stock_recommendations_date ON stock_recommendations ("날짜");

-- updated_at 자동 갱신 트리거(안전망). 현재 저장은 delete→insert 라 INSERT 시 DEFAULT NOW()로 채워지지만,
-- 향후 직접 UPDATE 경로가 생겨도 갱신 시각이 정확하도록 트리거를 둔다.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_stock_recommendations_updated_at ON stock_recommendations;
CREATE TRIGGER trg_stock_recommendations_updated_at
    BEFORE UPDATE ON stock_recommendations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- RLS 비활성화(내부 서비스키 전용 — market_data/investor_flow 와 동일 정책).
ALTER TABLE stock_recommendations DISABLE ROW LEVEL SECURITY;
