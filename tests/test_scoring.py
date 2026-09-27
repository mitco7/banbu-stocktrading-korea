"""scoring_service 검증 — 서브스코어 변환식, 하드필터, VIX 게이트(look-ahead), tie-breaker, LLM fail-close."""

from __future__ import annotations

import pytest

from core.config import settings
from services import llm_review_service, scoring_service as sc


# ── 서브스코어 ─────────────────────────────────────────────────────────────
def test_ml_sub_saturates():
    assert sc.ml_sub(0) == pytest.approx(0.0)
    assert sc.ml_sub(settings.ML_RISE_CAP / 2) == pytest.approx(0.5)
    assert sc.ml_sub(settings.ML_RISE_CAP) == pytest.approx(1.0)
    assert sc.ml_sub(settings.ML_RISE_CAP * 3) == pytest.approx(1.0)  # cap


def test_rsi_pt_piecewise():
    assert sc.rsi_pt(25) == pytest.approx(0.5)        # 딥 과매도
    assert sc.rsi_pt(30) == pytest.approx(0.5)
    assert sc.rsi_pt(35) == pytest.approx(0.75)       # 30~40 선형 중간
    assert sc.rsi_pt(40) == pytest.approx(1.0)
    assert sc.rsi_pt(60) == pytest.approx(1.0)        # 건강한 매수권
    assert sc.rsi_pt(75) == pytest.approx(1.0)
    assert sc.rsi_pt(78.5) == pytest.approx(0.8)      # 75~82 선형 감점 중간
    assert sc.rsi_pt(82) == pytest.approx(0.6)


def test_adx_pt_direction_guard():
    # 상승추세(+DI>-DI): 세기 인정
    assert sc.adx_pt(settings.ADX_CAP, 30, 10) == pytest.approx(1.0)
    assert sc.adx_pt(settings.ADX_CAP / 2, 30, 10) == pytest.approx(0.5)
    # 하락추세(-DI>+DI): ADX 높아도 0 (방향 가드)
    assert sc.adx_pt(settings.ADX_CAP, 10, 30) == pytest.approx(0.0)


def test_tech_sub_full_marks():
    s = sc.tech_sub(golden_cross=True, macd_buy=True, rsi=60, adx=settings.ADX_CAP, plus_di=30, minus_di=10)
    assert s == pytest.approx(1.0)


def test_flow_sub_confluence_penalty():
    # 합산점수 동일(flow_score=0.2 → base 0.6)이라도 외국인·기관 이견이면 0.7배.
    agree = sc.flow_sub(0.2, foreign_net=1e9, institution_net=2e9)
    disagree = sc.flow_sub(0.2, foreign_net=1e9, institution_net=-2e9)
    assert agree == pytest.approx(0.6)
    assert disagree == pytest.approx(0.6 * settings.CONFLUENCE_MULT)


def test_flow_signals_dual_buy():
    assert sc.flow_signals(1e9, 2e9)["dual_buy"] is True
    assert sc.flow_signals(1e9, -2e9)["dual_buy"] is False
    assert sc.flow_signals(1e9, -2e9)["disagree"] is True


def test_vol_sub_caps():
    assert sc.vol_sub(1.0) == pytest.approx(0.0)
    assert sc.vol_sub(settings.VOL_RATIO_CAP) == pytest.approx(1.0)
    assert sc.vol_sub(settings.VOL_RATIO_CAP * 2) == pytest.approx(1.0)  # 상단 cap
    assert sc.vol_sub(2.0) == pytest.approx((2.0 - 1) / (settings.VOL_RATIO_CAP - 1))


def test_ext20():
    assert sc.ext20(110, 100) == pytest.approx(0.1)
    assert sc.ext20(100, 0) is None


def test_total_score_weighted_sum():
    assert sc.total_score(1, 1, 1, 1) == pytest.approx(
        settings.W_ML + settings.W_TECH + settings.W_FLOW + settings.W_VOL
    )  # 합=1.0


# ── 하드필터 ───────────────────────────────────────────────────────────────
def _good_row(**over) -> dict:
    row = {
        "ml_recommendation": "BUY", "flow_score": 0.3, "rsi": 55,
        "close_price": 105, "sma_short": 100, "atr_pct": 5.0,
        "golden_cross": True, "macd_buy": True,
        "indicator_date": "2026-06-12", "flow_calc_date": "2026-06-12",
        "predicted_rise_pct": 8.0,
    }
    row.update(over)
    return row


def test_hard_filter_pass():
    assert sc.hard_filter(_good_row(), effective_min_rise=3.0, ml_asof="2026-06-12") is None


@pytest.mark.parametrize("over,expected", [
    ({"ml_recommendation": "SELL"}, "ml_sell"),
    ({"flow_score": None}, "flow_missing"),
    ({"rsi": None}, "no_indicators"),
    ({"rsi": 85}, "rsi_overheated"),
    ({"close_price": 130, "sma_short": 100}, "ext20_overheated"),   # 이격 30% > 25%
    ({"atr_pct": 25}, "atr_extreme"),
    ({"golden_cross": False, "macd_buy": False, "rsi": 35}, "tech_weak"),  # 0 hit
    ({"predicted_rise_pct": 4.0}, "below_caution_min"),            # 보수모드 5% 미달
])
def test_hard_filter_rejections(over, expected):
    reason = sc.hard_filter(_good_row(**over), effective_min_rise=5.0, ml_asof="2026-06-12")
    assert reason == expected


def test_hard_filter_asof_gap():
    # ML 기준일이 지표/수급보다 영업일 격차 초과 → fail-close.
    r = sc.hard_filter(_good_row(), effective_min_rise=3.0, ml_asof="2026-06-01")
    assert r is not None and r.startswith("asof_gap")


def test_hard_filter_counts_holidays_as_non_business_days():
    """연휴 직후: 직전 거래일 데이터는 stale 이 아니다(2026-09-28 전 종목 오탈락 사고).

    9/24·25 는 추석 휴장 → 9/23 지표와 9/28 ML 의 격차는 달력 평일로 3일이지만 영업일로는 1일.
    """
    row = _good_row(indicator_date="2026-09-23", flow_calc_date="2026-09-23")
    assert sc.hard_filter(row, effective_min_rise=3.0, ml_asof="2026-09-28") is None
    assert sc._busday_gap(["2026-09-23", "2026-09-28"]) == 1
    # 휴장일이 없는 구간은 기존과 동일하게 센다(완화가 아니라 달력 정정).
    assert sc._busday_gap(["2026-09-21", "2026-09-23"]) == 2
    # 진짜 오래된 데이터는 여전히 막는다.
    stale = _good_row(indicator_date="2026-09-16", flow_calc_date="2026-09-16")
    assert (sc.hard_filter(stale, effective_min_rise=3.0, ml_asof="2026-09-28") or "").startswith("asof_gap")


# ── VIX 게이트 (look-ahead 차단) ───────────────────────────────────────────
def _patch_vix(monkeypatch, values: list[float]):
    # 평일 날짜에 values 매핑(오름차순). fetch_series_before 는 당일 미만만 반환.
    dates = ["2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12"][-len(values):]
    rows = [{"날짜": d, "VIX 지수": v} for d, v in zip(dates, values)]
    monkeypatch.setattr(sc.market_data_repo, "fetch_series_before", lambda col, before, limit=15: rows)


def test_vix_gate_normal(monkeypatch):
    _patch_vix(monkeypatch, [18, 19, 20])
    regime, vix, reason = sc.vix_gate("2026-06-15")
    assert regime == "normal" and vix == pytest.approx(20) and reason is None


def test_vix_gate_caution(monkeypatch):
    _patch_vix(monkeypatch, [22, 24, 27])
    regime, vix, _ = sc.vix_gate("2026-06-15")
    assert regime == "caution" and vix == pytest.approx(27)


def test_vix_gate_block(monkeypatch):
    _patch_vix(monkeypatch, [28, 29, 32])
    regime, _, reason = sc.vix_gate("2026-06-15")
    assert regime == "blocked" and reason == "vix_block"


def test_vix_gate_missing_fail_close(monkeypatch):
    monkeypatch.setattr(sc.market_data_repo, "fetch_series_before", lambda col, before, limit=15: [])
    regime, vix, reason = sc.vix_gate("2026-06-15")
    assert regime == "blocked" and vix is None and reason == "vix_missing"


def test_vix_gate_ffill_stall_fail_close(monkeypatch):
    _patch_vix(monkeypatch, [21.5, 21.5, 21.5])   # 직전 3영업일 동일 → ffill 정체 의심
    regime, _, reason = sc.vix_gate("2026-06-15")
    assert regime == "blocked" and reason == "vix_stall"


# ── tie-breaker (결정적) ───────────────────────────────────────────────────
def test_sort_key_deterministic_tiebreak():
    a = {"total_score": 0.8, "atr_pct": 12.0, "predicted_rise_pct": 5, "flow_score": 0.1, "ticker": "000660"}
    b = {"total_score": 0.8, "atr_pct": 7.0, "predicted_rise_pct": 5, "flow_score": 0.1, "ticker": "005930"}
    # 동점 → atr_pct 작은 b 가 먼저.
    ordered = sorted([a, b], key=sc._sort_key)
    assert [x["ticker"] for x in ordered] == ["005930", "000660"]


# ── LLM fail-close ─────────────────────────────────────────────────────────
def test_llm_review_no_api_key_fail_close(monkeypatch):
    monkeypatch.setattr(llm_review_service.settings, "ANTHROPIC_API_KEY", "")
    ok, analysis, decisions, model = llm_review_service.review(
        [{"ticker": "005930", "name": "삼성전자"}], vix_t1=20, regime="normal", market_date="2026-06-15"
    )
    assert ok is False and decisions == {} and model is None


def test_llm_review_empty_survivors_ok():
    ok, analysis, decisions, model = llm_review_service.review([], vix_t1=20, regime="normal", market_date="2026-06-15")
    assert ok is True and decisions == {}
