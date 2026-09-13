"""매수 안전성 회귀 테스트 — 중복주문 차단, 주문실패 처리, 1회만 주문(재시도 없음),
무인 운영 가드(휴장일·주문시간창·stale 결정), 체결여부 불명 처리.

외부(KIS/DB/Slack)는 전부 모킹. 거래 사고 직결 경로라 동작을 고정한다(§2.7).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from db.repositories import trade_repo
from services import trading_service as ts

# 2026-06-12(금) 09:30 KST — 거래일 + 매수 주문 시간창(09:01~15:00) 안.
_FAKE_NOW = datetime(2026, 6, 12, 9, 30, tzinfo=ts.KST)
_FAKE_TODAY = "2026-06-12"


@pytest.fixture
def patched(monkeypatch):
    calls = {"orders": 0, "updates": []}

    monkeypatch.setattr(ts, "_now_kst", lambda: _FAKE_NOW)
    monkeypatch.setattr(ts.notification_service, "notify", lambda *a, **k: False)  # Slack 실발송 차단
    monkeypatch.setattr(ts.decision_repo, "latest_date", lambda: _FAKE_TODAY)
    monkeypatch.setattr(ts.decision_repo, "fetch_by_date",
                        lambda d: [{"ticker": "005930", "name": "삼성전자", "decision": "BUY"}])
    monkeypatch.setattr(ts.balance_service, "fetch_balance",
                        lambda: {"holdings": {}, "cash": 10_000_000, "raw_holdings": []})
    monkeypatch.setattr(ts.trade_repo, "open_tickers", lambda env: set())
    monkeypatch.setattr(ts.balance_service, "fetch_price",
                        lambda t: {"price": 70000, "prev_close": 70000, "change_pct": 0.0, "volume": 1.0})
    monkeypatch.setattr(ts.balance_service, "fetch_orderable",
                        lambda t, p: {"cash": 10_000_000, "max_qty": 100})
    monkeypatch.setattr(ts, "_compute_atr_asof", lambda code, asof: 1000.0)
    monkeypatch.setattr(ts.trade_repo, "update_record",
                        lambda rid, fields: calls["updates"].append((rid, fields)))

    def fake_order(path, tr_id, body):
        calls["orders"] += 1
        return {"output": {"ODNO": "0000123456"}}

    monkeypatch.setattr(ts.kis_client, "post_order", fake_order)
    return calls


def test_buy_happy_path_orders_once(patched, monkeypatch):
    monkeypatch.setattr(ts.trade_repo, "insert_record", lambda rec: 1)
    res = ts.execute_buys(_FAKE_TODAY)
    assert res["ordered"] == 1
    assert patched["orders"] == 1                      # 정확히 1회 주문(재시도 없음)
    assert patched["updates"][0][1].get("buy_order_id") == "0000123456"


def test_buy_duplicate_skips_without_ordering(patched, monkeypatch):
    def raise_dup(rec):
        raise trade_repo.DuplicatePositionError("dup")
    monkeypatch.setattr(ts.trade_repo, "insert_record", raise_dup)
    res = ts.execute_buys(_FAKE_TODAY)
    assert res["ordered"] == 0
    assert patched["orders"] == 0                      # 중복 → 주문 절대 안 나감
    assert ("삼성전자", "duplicate_open") in res["skipped"]


def test_buy_order_failure_marks_buy_failed(patched, monkeypatch):
    monkeypatch.setattr(ts.trade_repo, "insert_record", lambda rec: 7)

    def fail_order(path, tr_id, body):
        patched["orders"] += 1
        raise ts.kis_client.KisError("rejected")
    monkeypatch.setattr(ts.kis_client, "post_order", fail_order)

    res = ts.execute_buys(_FAKE_TODAY)
    assert res["ordered"] == 0
    assert patched["orders"] == 1                      # 1회 시도 후 실패(재시도 없음)
    assert (7, {"status": "buy_failed"}) in patched["updates"]


def test_buy_uncertain_keeps_buy_ordered_no_retry(patched, monkeypatch):
    """통신 예외(체결여부 불명) — buy_failed 로 단정하지 않고 buy_ordered 유지(재주문 금지)."""
    monkeypatch.setattr(ts.trade_repo, "insert_record", lambda rec: 9)

    def uncertain_order(path, tr_id, body):
        patched["orders"] += 1
        raise ts.kis_client.KisOrderUncertainError("timeout")
    monkeypatch.setattr(ts.kis_client, "post_order", uncertain_order)

    res = ts.execute_buys(_FAKE_TODAY)
    assert res["ordered"] == 0
    assert patched["orders"] == 1
    assert ("삼성전자", "order_uncertain") in res["skipped"]
    # buy_failed 로 바꾸는 update 가 없어야 한다(상태는 buy_ordered 유지 → reconcile 이 확정).
    assert all(f.get("status") != "buy_failed" for _rid, f in patched["updates"])


def test_buy_blocked_on_stale_decisions(patched, monkeypatch):
    """오늘 결정이 아니면 주문 금지(fail-close) — 어제 결정의 지연 집행 차단."""
    res = ts.execute_buys("2026-06-11")
    assert res["status"] == "stale_decisions"
    assert patched["orders"] == 0


def test_buy_blocked_on_weekend(patched, monkeypatch):
    """주말/휴장일에는 주문 자체를 스킵(스케줄러가 휴일에 돌아도 안전)."""
    monkeypatch.setattr(ts, "_now_kst",
                        lambda: datetime(2026, 6, 13, 9, 30, tzinfo=ts.KST))  # 토요일
    res = ts.execute_buys()
    assert res["status"] == "market_closed"
    assert patched["orders"] == 0


def test_buy_blocked_outside_order_window(patched, monkeypatch):
    """주문 시간창(09:01~15:00) 밖이면 스킵 — 동시호가/장외 시세로 주문 금지."""
    monkeypatch.setattr(ts, "_now_kst",
                        lambda: datetime(2026, 6, 12, 8, 45, tzinfo=ts.KST))
    res = ts.execute_buys()
    assert res["status"] == "outside_order_window"
    assert patched["orders"] == 0


def test_buy_skips_when_atr_unavailable(patched, monkeypatch):
    """ATR 미산출이면 매수 진입 금지(fail-close) — TP/SL 없는 포지션은 다음 매도 사이클이
    risk_unmanaged 로 즉시 청산해 스프레드·수수료만 태우는 왕복이 되기 때문."""
    monkeypatch.setattr(ts, "_compute_atr_asof", lambda code, asof: None)
    inserted: list[dict] = []
    monkeypatch.setattr(ts.trade_repo, "insert_record", lambda rec: inserted.append(rec) or 1)
    res = ts.execute_buys(_FAKE_TODAY)
    assert res["ordered"] == 0
    assert patched["orders"] == 0                      # 주문 절대 안 나감
    assert ("삼성전자", "atr_unavailable") in res["skipped"]
    assert inserted == []                              # 주문 의도(INSERT)조차 기록 안 함


# ── 매도 이중 주문 차단(CAS 선점) ──────────────────────────────────────────
def test_market_sell_claim_failure_blocks_order(monkeypatch):
    """holding→sell_ordered 선점 실패(동시 실행 경합) 시 주문을 절대 보내지 않는다(§2.7)."""
    orders: list[int] = []
    monkeypatch.setattr(ts.notification_service, "notify", lambda *a, **k: False)
    monkeypatch.setattr(ts.kis_client, "post_order",
                        lambda *a: orders.append(1) or {"output": {"ODNO": "1"}})
    monkeypatch.setattr(ts.trade_repo, "claim_transition", lambda rid, frm, fields: False)
    rec = {"id": 1, "ticker": "005930", "name": "삼성전자", "quantity": 1}
    assert ts._market_sell(rec, "stop_loss") is False
    assert orders == []


def test_market_sell_claims_before_order(monkeypatch):
    """선점(CAS)이 주문 송신보다 먼저 일어난다 — 송신 후 전이는 경합 창을 남긴다."""
    events: list[str] = []
    monkeypatch.setattr(ts.notification_service, "notify", lambda *a, **k: False)
    monkeypatch.setattr(ts.trade_repo, "claim_transition",
                        lambda rid, frm, fields: events.append("claim") or True)
    monkeypatch.setattr(ts.trade_repo, "update_record",
                        lambda rid, fields: events.append("update"))
    monkeypatch.setattr(ts.kis_client, "post_order",
                        lambda *a: events.append("order") or {"output": {"ODNO": "1"}})
    rec = {"id": 2, "ticker": "005930", "name": "삼성전자", "quantity": 1}
    assert ts._market_sell(rec, "take_profit") is True
    assert events[0] == "claim" and events.index("claim") < events.index("order")


def test_market_sell_failure_rolls_back_to_holding(monkeypatch):
    """확정 거부(KisError) 시 holding 복귀 — 다음 사이클이 재점검할 수 있어야 한다."""
    updates: list[tuple] = []
    monkeypatch.setattr(ts.notification_service, "notify", lambda *a, **k: False)
    monkeypatch.setattr(ts.trade_repo, "claim_transition", lambda rid, frm, fields: True)
    monkeypatch.setattr(ts.trade_repo, "update_record",
                        lambda rid, fields: updates.append((rid, fields)))

    def fail(*a):
        raise ts.kis_client.KisError("rejected")
    monkeypatch.setattr(ts.kis_client, "post_order", fail)
    rec = {"id": 3, "ticker": "005930", "name": "삼성전자", "quantity": 1}
    assert ts._market_sell(rec, "stop_loss") is False
    assert (3, {"status": "holding"}) in updates


def test_market_sell_uncertain_keeps_sell_ordered(monkeypatch):
    """체결여부 불명 시 sell_ordered 유지(재전송 금지) — holding 복귀 업데이트가 없어야 한다."""
    updates: list[tuple] = []
    monkeypatch.setattr(ts.notification_service, "notify", lambda *a, **k: False)
    monkeypatch.setattr(ts.trade_repo, "claim_transition", lambda rid, frm, fields: True)
    monkeypatch.setattr(ts.trade_repo, "update_record",
                        lambda rid, fields: updates.append((rid, fields)))

    def uncertain(*a):
        raise ts.kis_client.KisOrderUncertainError("timeout")
    monkeypatch.setattr(ts.kis_client, "post_order", uncertain)
    rec = {"id": 4, "ticker": "005930", "name": "삼성전자", "quantity": 1}
    assert ts._market_sell(rec, "stop_loss") is False
    assert all(f.get("status") != "holding" for _rid, f in updates)


def test_sell_cycle_reconcile_throttled(monkeypatch):
    """장중 reconcile 은 설정 간격(기본 300초)에 1회만 — 매 사이클 잔고 조회로 인한
    모의서버 부하·레이트리밋 완화. 연속 사이클에서 반복 호출되면 안 된다."""
    monkeypatch.setattr(ts, "_now_kst", lambda: _FAKE_NOW)
    monkeypatch.setattr(ts.market_calendar, "is_market_hours", lambda now: True)
    monkeypatch.setattr(ts.trade_repo, "fetch_holdings", lambda env: [])
    calls: list[int] = []
    monkeypatch.setattr(ts, "reconcile", lambda: calls.append(1))
    ts._last_reconcile_mono = 0.0  # 기동 직후 상태 — 첫 사이클은 즉시 실행돼야 함
    ts.run_sell_cycle()
    ts.run_sell_cycle()
    ts.run_sell_cycle()
    assert calls == [1]


# ── 매도 감시 루프 종목별 격리 ─────────────────────────────────────────────
def test_sell_cycle_isolates_per_stock_errors(monkeypatch):
    """한 종목의 시세 조회 실패가 나머지 종목의 손절 판정을 막으면 안 된다(최종 안전망)."""
    monkeypatch.setattr(ts, "_now_kst", lambda: _FAKE_NOW)
    monkeypatch.setattr(ts.market_calendar, "is_market_hours", lambda now: True)
    monkeypatch.setattr(ts, "reconcile", lambda: {"transitions": [], "status": "ok"})
    monkeypatch.setattr(ts.notification_service, "notify", lambda *a, **k: False)
    holdings = [
        {"id": 1, "ticker": "000001", "name": "A", "status": "holding", "quantity": 1,
         "buy_price": 100.0, "buy_date": _FAKE_TODAY, "take_profit": 200.0, "stop_loss": 90.0},
        {"id": 2, "ticker": "000002", "name": "B", "status": "holding", "quantity": 1,
         "buy_price": 100.0, "buy_date": _FAKE_TODAY, "take_profit": 200.0, "stop_loss": 90.0},
    ]
    monkeypatch.setattr(ts.trade_repo, "fetch_holdings", lambda env: holdings)
    monkeypatch.setattr(ts, "_vix_t1", lambda today: None)
    monkeypatch.setattr(ts.flow_repo, "fetch_latest_snapshot", lambda: [])
    monkeypatch.setattr(ts, "_confirmed_daily_bars", lambda t, d: None)

    def price(t):
        if t == "000001":
            raise ts.kis_client.KisError("시세 조회 불가")
        return {"price": 80.0, "prev_close": 100.0, "change_pct": 0.0, "volume": 1.0}  # SL 히트
    monkeypatch.setattr(ts.balance_service, "fetch_price", price)

    sells: list[tuple] = []
    monkeypatch.setattr(ts, "_market_sell", lambda rec, reason: sells.append((rec["ticker"], reason)) or True)

    res = ts.run_sell_cycle()
    assert res["errors"] == ["000001"]                 # 실패 종목은 기록만
    assert ("000002", "stop_loss") in sells            # 뒤 종목 SL 판정은 계속 동작
    assert res["sold"] == 1
