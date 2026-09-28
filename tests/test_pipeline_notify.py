"""매수 알림 문구 검증 — 주문 집행 스위치에 따라 '오늘 집행' / '추천만' 안내가 달라야 한다."""

from __future__ import annotations

import pytest

from core.config import settings
from services import pipeline_service as ps

_DETAILS = {
    "llm_market_analysis": "변동성 낮음",
    "buy": {"count": 1, "stocks": [{
        "ticker": "005930", "name": "삼성전자", "reason": "수급 쌍끌이와 골든크로스가 겹침",
        "llm_model": "claude-opus-4-8",
        "scores": {"total_score": 0.62, "ml_sub": 0.5, "tech_sub": 0.7,
                   "flow_sub": 0.6, "vol_sub": 0.6, "dual_buy": True},
        "ml_prediction": {"last_actual_price": 100_000.0, "predicted_future_price": 108_000.0,
                          "predicted_rise_pct": 8.0, "rise_threshold": 3.0},
        "investor_flow": {"foreign_net": 1.2e11, "institution_net": 4.0e10,
                          "flow_score": 0.31, "flow_calc_date": "2026-09-23"},
        "technical_indicators": {
            "atr": 3_000.0, "atr_pct": 3.0, "indicator_date": "2026-09-23",
            "sma_short": 98_000.0, "sma_long": 95_000.0, "golden_cross": True,
            "macd": 820.0, "macd_signal": 640.0, "macd_buy": True,
            "rsi": 55.2, "rsi_signal": "neutral",
            "adx": 27.4, "plus_di": 24.1, "minus_di": 18.3, "trend_strong": True,
            "volume_ratio": 1.35,
        },
    }]},
    "hold": {"count": 2}, "non_candidates": {"count": 3},
}
_DECISION = {"vix_t1": 15.0, "regime": "normal", "buys": 1}


def test_summary_explains_every_scoring_input(summary):
    """추천 근거는 ML·수급·기술지표 세부항목·점수 산식·AI 사유·매매계획이 모두 보여야 한다."""
    text = summary(True)
    # 매매 계획: 기준가 → 익절/손절과 ATR 근거
    assert "매수 기준가 100,000원" in text
    assert "익절 109,000원(+9.0%)" in text and "손절 92,500원(-7.5%)" in text
    assert "ATR 3,000원(주가의 3.00%)" in text and "익절 ATR×3 / 손절 ATR×2.5" in text
    # ① ML  ② 수급  ③ 기술지표 세부항목  ④ 변동성
    assert "① ML 예측:" in text and "108,000원 (+8.00%, 후보 기준 3.0% 이상)" in text
    assert "② 수급(2026-09-23 기준 5거래일 합): 외국인 +1,200억 · 기관 +400억" in text
    assert "쌍끌이" in text and "flow_score +0.31" in text
    assert "③ 기술지표:" in text
    for bit in ("골든크로스 O(SMA20 98,000 vs SMA50 95,000)", "MACD 매수 O(820 vs 시그널 640)",
                "RSI 55.2(중립)", "ADX 27.4 추세 O(+DI 24.1 / -DI 18.3)", "거래량 평소의 1.35배"):
        assert bit in text, bit
    assert "④ 변동성: ATR 3.00%" in text
    # 점수 산식과 AI 근거
    assert "종합점수 산식: ML 0.50×30% + 기술 0.70×30% + 수급 0.60×25% + 변동성 0.60×15% = 0.620" in text
    assert "AI 최종 검토(claude-opus-4-8): 수급 쌍끌이와 골든크로스가 겹침" in text


def test_summary_survives_missing_indicator_values(summary, monkeypatch):
    """지표 일부가 NULL 이어도 알림은 깨지지 않고 있는 값만 보여준다(fail-soft)."""
    bare = {
        "llm_market_analysis": None,
        "buy": {"count": 1, "stocks": [{
            "ticker": "000660", "name": "SK하이닉스",
            "scores": {"total_score": 0.5}, "ml_prediction": {}, "investor_flow": {},
            "technical_indicators": {},
        }]},
        "hold": {"count": 0}, "non_candidates": {"count": 0},
    }
    monkeypatch.setattr(settings, "TRADING_ENABLED", True)
    text = ps._buy_summary(bare, _DECISION, market_blocked=False)
    assert "SK하이닉스(000660)" in text and "종합점수 0.500" in text


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


def test_summary_explains_why_no_buy_today(summary, monkeypatch):
    """매수 0건인 날엔 AI 검토까지 갔다 보류된 상위 후보의 사유를 함께 알린다."""
    monkeypatch.setattr(settings, "TRADING_ENABLED", True)
    details = {
        "llm_market_analysis": "저변동성 국면",
        "buy": {"count": 0, "stocks": []},
        "hold": {"count": 3, "stocks": [
            {"ticker": "329180", "name": "HD현대중공업", "stage": "llm_reviewed",
             "reason": "골든크로스 미형성에 기관 대량 순매도", "scores": {"total_score": 0.325}},
            {"ticker": "017670", "name": "SK텔레콤", "stage": "llm_reviewed",
             "reason": "추세 미약, 거래량 저조", "scores": {"total_score": 0.294}},
            {"ticker": "012330", "name": "현대모비스", "stage": "hard_filtered",
             "reason": "tech_weak", "scores": {"total_score": None}},
        ]},
        "non_candidates": {"count": 21},
    }
    text = ps._buy_summary(details, {"vix_t1": 14.9, "regime": "normal", "buys": 0},
                           market_blocked=False)
    assert "매수 종목 없음" in text and "보류된 상위 후보" in text
    assert "HD현대중공업(329180) 종합점수 0.325 — 골든크로스 미형성에 기관 대량 순매도" in text
    assert "SK텔레콤(017670)" in text
    assert "현대모비스" not in text          # 하드필터 탈락은 AI 검토 전이라 제외
