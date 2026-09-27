"""KIS 매매 순수 로직 검증 — 호가단위, ATR TP/SL, 매도 트리거(손절 우선·VIX·time-stop), 신호 카운트."""

from __future__ import annotations

import pandas as pd
import pytest

from core.config import settings
from services import trading_service as ts
from utils import hoga


# ── 호가단위 ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("price,tick", [
    (1500, 1), (3000, 5), (15000, 10), (30000, 50),
    (100000, 100), (300000, 500), (600000, 1000),
])
def test_tick_size(price, tick):
    assert hoga.tick_size(price) == tick


def test_round_to_tick_nearest():
    assert hoga.round_to_tick(100040) == 100000   # 호가 100, 내림
    assert hoga.round_to_tick(100060) == 100100   # 올림
    assert hoga.round_to_tick(15003) == 15000     # 호가 10


def test_price_limits_and_clamp():
    upper, lower = hoga.price_limits(100000)       # ±30%
    assert upper == 130000 and lower == 70000
    assert hoga.clamp_to_limit(140000, 100000) == 130000   # 상한 클램프
    assert hoga.clamp_to_limit(50000, 100000) == 70000     # 하한 클램프
    assert hoga.within_price_limit(130000, 100000) is True
    assert hoga.within_price_limit(130001, 100000) is False


# ── ATR TP/SL ──────────────────────────────────────────────────────────────
def test_compute_tp_sl():
    tp, sl = ts.compute_tp_sl(100000, 2000)
    assert tp == pytest.approx(100000 + 2000 * settings.TP_ATR_MULT)   # +8000
    assert sl == pytest.approx(100000 - 2000 * settings.SL_ATR_MULT)   # -5000
    assert tp > sl


def test_compute_tp_sl_none_atr():
    assert ts.compute_tp_sl(100000, None) == (None, None)
    assert ts.compute_tp_sl(100000, 0) == (None, None)


def test_busdays():
    assert ts._busdays("2026-06-08", "2026-06-22") == 10   # 영업일 수


# ── 매도 결정(우선순위·임계) ───────────────────────────────────────────────
def test_decide_sell_stop_priority():
    # 손절·익절 동시 도달 → 손절 우선.
    should, reason = ts.decide_sell(sl_hit=True, tp_hit=True, time_stop=False,
                                    signal_count=0, flow_score=0.0, vix_t1=None)
    assert should and reason == "stop_loss"


def test_decide_sell_take_profit():
    should, reason = ts.decide_sell(sl_hit=False, tp_hit=True, time_stop=False,
                                    signal_count=0, flow_score=0.0, vix_t1=None)
    assert should and reason == "take_profit"


def test_decide_sell_time_stop():
    should, reason = ts.decide_sell(sl_hit=False, tp_hit=False, time_stop=True,
                                    signal_count=0, flow_score=0.0, vix_t1=None)
    assert should and reason == "time_stop"


def test_decide_sell_signal_threshold():
    should, reason = ts.decide_sell(sl_hit=False, tp_hit=False, time_stop=False,
                                    signal_count=settings.SELL_SIGNAL_THRESHOLD, flow_score=0.0, vix_t1=10)
    assert should and reason == "signal"


def test_decide_sell_signal_flow():
    should, reason = ts.decide_sell(sl_hit=False, tp_hit=False, time_stop=False,
                                    signal_count=2, flow_score=-0.2, vix_t1=10)
    assert should and reason == "signal_flow"


def test_decide_sell_vix_block_one_signal():
    should, reason = ts.decide_sell(sl_hit=False, tp_hit=False, time_stop=False,
                                    signal_count=1, flow_score=0.0, vix_t1=31)
    assert should and reason == "signal_vix_block"


def test_decide_sell_vix_caution_two_signals():
    should, reason = ts.decide_sell(sl_hit=False, tp_hit=False, time_stop=False,
                                    signal_count=2, flow_score=0.0, vix_t1=26)
    assert should and reason == "signal_vix_caution"


def test_decide_sell_hold():
    should, reason = ts.decide_sell(sl_hit=False, tp_hit=False, time_stop=False,
                                    signal_count=1, flow_score=0.0, vix_t1=20)
    assert should is False and reason is None


# ── 매도 신호 카운트 ───────────────────────────────────────────────────────
def _daily(closes: list[float], volume: float = 1_000_000.0) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-06-22", periods=len(closes))
    c = pd.Series(closes, index=idx, dtype="float64")
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c,
                         "volume": pd.Series([volume] * len(closes), index=idx)})


def test_sell_signals_downtrend_with_panic():
    df = _daily([200 - i for i in range(70)])   # 하락추세
    quote = {"volume": 3_000_000, "change_pct": -5.0}  # 거래량 3배 + 급락 → 패닉
    sig = ts.evaluate_sell_signals(df, quote, flow_score=None)
    assert sig["dead_cross"] is True            # SMA20 < SMA60
    assert sig["macd_below"] is True
    assert sig["panic"] is True
    assert sig["rsi_over"] is False             # 하락추세라 과열 아님
    assert sig["count"] == 3


def test_sell_signals_uptrend_overbought():
    df = _daily([100 + i for i in range(70)])   # 강한 상승추세
    quote = {"volume": 1_000_000, "change_pct": 1.0}   # 패닉 아님
    sig = ts.evaluate_sell_signals(df, quote, flow_score=None)
    assert sig["rsi_over"] is True              # RSI > 82
    assert sig["dead_cross"] is False
    assert sig["panic"] is False
    assert sig["count"] == 1


# ── 매도 확정 거부 알림 쿨다운 ─────────────────────────────────────────────
def test_notify_sell_reject_cooldown(monkeypatch):
    """같은 종목·같은 거부 메시지는 쿨다운 안에 1건만 발송, 다른 종목/메시지는 별도 발송."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(ts.notification_service, "notify_error",
                        lambda title, msg="": sent.append((title, msg)) or True)
    monkeypatch.setattr(settings, "SELL_REJECT_NOTIFY_COOLDOWN_SEC", 1800)
    ts._last_sell_reject_notify.clear()

    rec = {"ticker": "066570", "name": "LG전자"}
    exc = ts.kis_client.KisError("주문 거부 rt_cd=1 40910000: 모의투자 주문이 불가한 계좌입니다.")

    ts._notify_sell_reject(rec, "time_stop", exc)
    ts._notify_sell_reject(rec, "time_stop", exc)
    ts._notify_sell_reject(rec, "time_stop", exc)
    assert len(sent) == 1
    assert "LG전자(066570)" in sent[0][1] and "40910000" in sent[0][1]

    # 다른 종목은 독립 키
    ts._notify_sell_reject({"ticker": "005930", "name": "삼성전자"}, "time_stop", exc)
    assert len(sent) == 2

    # 쿨다운 경과 후에는 다시 발송
    ts._last_sell_reject_notify[("066570", str(exc))] -= 1801
    ts._notify_sell_reject(rec, "time_stop", exc)
    assert len(sent) == 3

    # 쿨다운 0 이면 매번 발송(끄기)
    monkeypatch.setattr(settings, "SELL_REJECT_NOTIFY_COOLDOWN_SEC", 0)
    ts._notify_sell_reject(rec, "time_stop", exc)
    ts._notify_sell_reject(rec, "time_stop", exc)
    assert len(sent) == 5
