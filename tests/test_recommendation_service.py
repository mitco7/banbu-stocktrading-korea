"""recommendation_service 서비스 로직 검증 (외부 API/DB 모킹).

지표 순수함수는 test_indicators.py 가 담당. 여기서는 후보 선별·graceful 실패·멱등성·
§2.2(확률 아닌 상승률) 매핑·ML 신선도(fail-close) 를 회귀 방지한다.
"""

from __future__ import annotations

import pandas as pd
import pytest

from services import recommendation_service as rs

REC_DATE = "2026-06-12"
_FRESH_TS = "2026-06-12T06:00:00+00:00"  # rec_date 기준 신선


def _ohlcv(n: int = 90) -> pd.DataFrame:
    """완만한 상승 추세의 합성 일봉(거래일 인덱스). n봉."""
    idx = pd.bdate_range(end="2026-06-12", periods=n)
    close = pd.Series([100.0 + i for i in range(n)], index=idx)
    return pd.DataFrame({
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": pd.Series([1_000_000 + (i % 5) * 1000 for i in range(n)], index=idx),
    })


def _analysis_row(name: str, rise: float, ts: str = _FRESH_TS) -> dict:
    return {
        "Stock": name,
        "Last Actual Price": 100.0,
        "Predicted Future Price": 100.0 * (1 + rise / 100),
        "Predicted Rise": rise > 0,
        "Rise Probability (%)": rise,
        "Recommendation": "STRONG BUY" if rise > 2 else "BUY",
        "created_at": ts,
    }


@pytest.fixture
def patched(monkeypatch):
    """공통 모킹 + upsert 페이로드 캡처."""
    captured: dict = {}

    def fake_replace(date, records):
        captured["date"] = date
        captured["records"] = records
        return len(records)

    monkeypatch.setattr(rs.recommendation_repo, "replace_for_date", fake_replace)
    monkeypatch.setattr(rs.flow_repo, "fetch_latest_snapshot", lambda: [])
    monkeypatch.setattr(rs, "_fetch_ohlcv_safe", lambda code, s, e: _ohlcv())
    return captured


def test_stores_all_universe_with_candidate_flag(patched, monkeypatch):
    rows = [
        _analysis_row("삼성전자", 10.0),     # 후보(>=3%)
        _analysis_row("SK하이닉스", 1.0),    # 후보 아님 — 그래도 저장
        _analysis_row("외계종목", 50.0),     # 유니버스 밖 — 제외
    ]
    monkeypatch.setattr(rs.analysis_repo, "fetch_latest_analysis", lambda: rows)
    res = rs.generate_recommendations(ref_date=REC_DATE, min_rise_pct=3.0)
    assert res["stored"] == 2           # 유니버스 2종목 전부 저장
    assert res["candidates"] == 1       # 후보는 1종목
    by_name = {r["name"]: r for r in patched["records"]}
    assert set(by_name) == {"삼성전자", "SK하이닉스"}
    assert by_name["삼성전자"]["is_candidate"] is True
    assert by_name["SK하이닉스"]["is_candidate"] is False
    # 후보 아닌 종목도 지표가 채워져야 한다(전 종목 분석).
    assert by_name["SK하이닉스"]["close_price"] is not None


def test_insufficient_data_stored_with_null_indicators(patched, monkeypatch):
    rows = [_analysis_row("삼성전자", 10.0), _analysis_row("SK하이닉스", 8.0)]
    monkeypatch.setattr(rs.analysis_repo, "fetch_latest_analysis", lambda: rows)
    # SK하이닉스만 데이터 부족(None) → 행은 저장하되 지표 NULL.
    monkeypatch.setattr(
        rs, "_fetch_ohlcv_safe",
        lambda code, s, e: None if code == "000660" else _ohlcv(),
    )
    res = rs.generate_recommendations(ref_date=REC_DATE, min_rise_pct=3.0)
    assert res["stored"] == 2                       # 둘 다 저장
    assert "SK하이닉스" in res["no_indicators"]
    by_name = {r["name"]: r for r in patched["records"]}
    assert by_name["SK하이닉스"]["close_price"] is None
    assert by_name["SK하이닉스"]["adx"] is None
    assert by_name["삼성전자"]["close_price"] is not None


def test_no_flow_sets_all_flow_cols_null(patched, monkeypatch):
    """[C2 회귀] 수급 없는 레코드는 4개 수급 컬럼을 모두 None 으로 가져야 한다(멱등 키 통일)."""
    monkeypatch.setattr(rs.analysis_repo, "fetch_latest_analysis",
                        lambda: [_analysis_row("삼성전자", 10.0)])
    rs.generate_recommendations(ref_date=REC_DATE, min_rise_pct=3.0)
    rec = patched["records"][0]
    for col in ("flow_score", "foreign_net", "institution_net", "flow_calc_date"):
        assert col in rec and rec[col] is None


def test_flow_merged_when_present(patched, monkeypatch):
    monkeypatch.setattr(rs.analysis_repo, "fetch_latest_analysis",
                        lambda: [_analysis_row("삼성전자", 10.0)])
    monkeypatch.setattr(rs.flow_repo, "fetch_latest_snapshot", lambda: [{
        "ticker": "005930", "flow_score": 0.42, "foreign_net": 1e10,
        "institution_net": -2e9, "calculation_date": "2026-06-11",
    }])
    rs.generate_recommendations(ref_date=REC_DATE, min_rise_pct=3.0)
    rec = patched["records"][0]
    assert rec["flow_score"] == pytest.approx(0.42)
    assert rec["flow_calc_date"] == "2026-06-11"


def test_idempotent_same_date_same_keyset(patched, monkeypatch):
    """같은 날 두 번 호출 시 레코드 키 집합 동일(§2.7)."""
    monkeypatch.setattr(rs.analysis_repo, "fetch_latest_analysis",
                        lambda: [_analysis_row("삼성전자", 10.0)])
    rs.generate_recommendations(ref_date=REC_DATE, min_rise_pct=3.0)
    keys1 = set(patched["records"][0])
    rs.generate_recommendations(ref_date=REC_DATE, min_rise_pct=3.0)
    keys2 = set(patched["records"][0])
    assert keys1 == keys2
    assert all(r["날짜"] == REC_DATE for r in patched["records"])


def test_rise_pct_mapped_not_probability(patched, monkeypatch):
    """[§2.2 회귀] 'Rise Probability (%)' 값이 predicted_rise_pct 로 매핑되고 %단위로 필터된다."""
    monkeypatch.setattr(rs.analysis_repo, "fetch_latest_analysis",
                        lambda: [_analysis_row("삼성전자", 7.5)])
    rs.generate_recommendations(ref_date=REC_DATE, min_rise_pct=3.0)
    rec = patched["records"][0]
    assert rec["predicted_rise_pct"] == pytest.approx(7.5)
    assert rec["is_candidate"] is True
    assert "probability" not in {k.lower() for k in rec}  # 확률 표기 금지


def test_stale_ml_blocks_generation(patched, monkeypatch):
    """오래된 ML 결과면 후보 생성 중단(fail-close)."""
    old = _analysis_row("삼성전자", 10.0, ts="2026-05-01T06:00:00+00:00")
    monkeypatch.setattr(rs.analysis_repo, "fetch_latest_analysis", lambda: [old])
    res = rs.generate_recommendations(ref_date=REC_DATE, min_rise_pct=3.0)
    assert res["status"] == "stale_ml"
    assert res["stored"] == 0
    assert "records" not in patched  # upsert 호출 안 됨


def test_empty_ml_returns_no_data(patched, monkeypatch):
    monkeypatch.setattr(rs.analysis_repo, "fetch_latest_analysis", lambda: [])
    res = rs.generate_recommendations(ref_date=REC_DATE, min_rise_pct=3.0)
    assert res["status"] == "no_ml_data"
    assert res["stored"] == 0
