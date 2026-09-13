-- investor_flow: 외국인·기관 수급(순매수) 점수 (종목·기준일별).
-- flow_score 는 -1~+1 (양수=매수우위=호재성, 음수=매도우위=악재성). 뉴스감성 점수 자리 대체.
-- (ticker, calculation_date) 복합키로 **이력 보존** — 같은 기준일 재실행은 갱신(멱등),
--   새 기준일은 새 행. '현재 스냅샷'은 calculation_date 최대값으로 조회한다.
--   이력 보존 이유: 종목별 rolling 분포(z-score 정규화)·look-ahead 백테스트에 과거 점수 필요(stock-quant).
-- ⚠ 데이터 소스는 pykrx(KRX) 투자자별 순매수. KRX 접속이 막힌 환경에서는 채워지지 않는다.
-- ⚠ 점수 유효 시점: calculation_date(기준일) 데이터는 익일 아침 확정 → 의사결정엔 (기준일+1영업일)부터 사용.

CREATE TABLE IF NOT EXISTS investor_flow (
    ticker TEXT NOT NULL,                -- 종목코드 6자리
    name TEXT,                           -- 종목명(참고용)
    foreign_net FLOAT8,                  -- 외국인 순매수 합(원, lookback_days 거래일 합산)
    institution_net FLOAT8,              -- 기관 순매수 합(원)
    flow_score FLOAT8,                   -- -1 ~ +1 (감성 점수 자리)
    lookback_days INT,                   -- 합산 거래일 수
    calculation_date DATE NOT NULL,      -- 산출 기준일
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, calculation_date)
);

CREATE INDEX IF NOT EXISTS idx_investor_flow_calcdate ON investor_flow (calculation_date);

-- RLS 비활성화(내부 서비스키 전용 — 빈응답 재수집/조회누락 사고 방지, market_data 와 동일 정책).
ALTER TABLE investor_flow DISABLE ROW LEVEL SECURITY;
