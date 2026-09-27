"""매수 알림 문구 검증 — 주문 집행 스위치에 따라 '오늘 집행' / '추천만' 안내가 달라야 한다."""

from __future__ import annotations

import pytest

from core.config import settings
from services import pipeline_service as ps

_DETAILS = {
    "llm_market_analysis": "변동성 낮음",
    "buy": {"count": 1, "stocks": [{
        "ticker": "005930", "name": "삼성전자", "reason": "수급 개선",
        "scores": {"total_score": 0.62, "ml_sub": 0.5, "tech_sub": 0.7,
                   "flow_sub": 0.6, "vol_sub": 0.6, "dual_buy": True},
        "ml_prediction": {"last_actual_price": 100_000.0, "predicted_future_price": 108_000.0,
                          "predicted_rise_pct": 8.0},
        "investor_flow": {"foreign_net": 1.2e11, "institution_net": 4.0e10},
        "technical_indicators": {"atr": 3_000.0},
    }]},
    "hold": {"count": 2}, "non_candidates": {"count": 3},
}
_DECISION = {"vix_t1": 15.0, "regime": "normal", "buys": 1}


@pytest.fixture
def summary(monkeypatch):
    def build(trading_enabled: bool) -> str:
        monkeypatch.setattr(settings, "TRADING_ENABLED", trading_enabled)
        return ps._buy_summary(_DETAILS, _DECISION, market_blocked=False)
    return build


def test_summary_promises_execution_when_trading_enabled(summary):
    text = summary(True)
    assert f"매수는 오늘 {settings.SCHEDULE_BUY_TIME} 시장가 집행" in text
    assert "삼성전자(005930)" in text and "목표" in text and "손절" in text


def test_summary_says_recommendation_only_when_trading_disabled(summary):
    """주문이 꺼져 있는데 '오늘 집행'이라고 알리면 거짓 안내가 된다."""
    text = summary(False)
    assert "실제 매수·매도는 하지 않습니다" in text and "참고용 예상치" in text
    assert "시장가 집행" not in text
    assert "삼성전자(005930)" in text        # 추천 내용 자체는 그대로 발송
