-- trade_records: 매수/매도 체결 및 포지션 상태 (Step 6).
-- 상태 흐름: buy_ordered →(체결) holding →(매도신호) sell_ordered →(체결) sold / (미체결) buy_failed
-- env(PAPER/PROD)별로 분리. 한 포지션 = 한 행(부분체결은 단순화해 수량 갱신).
-- ⚠ 멱등성(§2.7): 같은 날 같은 종목 중복 매수 방지는 trading_service 가 '미청산 보유/주문중' 검사로 처리.
--    주문 자체는 재시도 금지(중복주문=최악) — 본 테이블은 주문 송신 전 INSERT 로 의도를 먼저 기록.

CREATE TABLE IF NOT EXISTS trade_records (
    id BIGSERIAL PRIMARY KEY,
    env TEXT NOT NULL,                   -- 'PAPER' / 'PROD'
    ticker TEXT NOT NULL,                -- 종목코드 6자리
    name TEXT,

    status TEXT NOT NULL,                -- buy_ordered/holding/sell_ordered/sold/buy_failed
    quantity INT,                        -- 보유/주문 수량(주)

    -- 매수
    buy_order_id TEXT,                   -- KIS 주문번호(ODNO)
    buy_price FLOAT8,                    -- 체결가(또는 주문 지정가)
    buy_date DATE,                       -- 매수 체결일(KST) — ATR 백필 기준일
    buy_ordered_at TIMESTAMPTZ,

    -- 익절/손절 (KRW, ATR 기반). NULL 이면 매도 사이클에서 buy_date 기준 백필.
    take_profit FLOAT8,
    stop_loss FLOAT8,
    atr FLOAT8,                          -- 기록용(buy_date 기준 ATR)
    risk_unmanaged BOOLEAN DEFAULT FALSE,-- ATR 백필 실패 → 다음 영업일 시장가 청산 후보

    -- 매도
    sell_order_id TEXT,
    sell_price FLOAT8,
    sell_date DATE,
    sell_reason TEXT,                    -- take_profit/stop_loss/time_stop/signal/panic 등
    profit_loss FLOAT8,                  -- 손익(원)
    profit_loss_pct FLOAT8,             -- 손익률(%)

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 미청산 포지션(보유/주문중) 빠른 조회.
CREATE INDEX IF NOT EXISTS idx_trade_records_open
    ON trade_records (env, ticker, status);

-- ★ 멱등성 최종 방어선(§2.7): 같은 env·종목으로 미청산 포지션이 둘 이상 생기지 못하게 한다.
-- 동시 호출/스케줄러 중복/reconcile 고아 INSERT 가 메모리 dedup 을 뚫어도 DB가 중복 주문·중복 포지션을 거부.
-- sold/buy_failed 는 제외 → 매도 후 재매수 허용. ("같은 종목 하루 한 포지션" 정책 — dedup 설계와 일치.)
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_records_open_ticker
    ON trade_records (env, ticker)
    WHERE status IN ('buy_ordered', 'holding', 'sell_ordered');

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_trade_records_updated_at ON trade_records;
CREATE TRIGGER trg_trade_records_updated_at
    BEFORE UPDATE ON trade_records
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE trade_records DISABLE ROW LEVEL SECURITY;
