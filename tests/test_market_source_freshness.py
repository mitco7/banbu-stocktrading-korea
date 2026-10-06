"""소스 신선도 가드 + 네이버 지수 파서 회귀 테스트.

2026-10-06 사고: FDR 의 KRX 지수가 9/17 자에서 고착됐는데 응답 자체는 정상이라
수집기가 '성공'으로 기록 → 18영업일간 ffill 로 덮였다(실제값 대비 KOSPI 3.8%, KOSDAQ 11.3% 차이).
여기서 고정하는 것:
  1) 고착 소스를 실패로 보고 다음 폴백으로 넘어가는가
  2) 백필·연휴·저빈도 지표에서 오탐하지 않는가
  3) 네이버 응답의 None(거래정지·VI 등)과 EUC-KR 인코딩을 견디는가
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.market_spec import (
    FREQ_DAILY,
    FREQ_MONTHLY,
    MAX_SOURCE_STALE_DAYS,
    TIER_REQUIRED,
    Series,
)
from services import market_data_service as mds
from services.collectors import price_sources as ps


def _series(freq: str = FREQ_DAILY, sources: tuple = ()) -> Series:
    return Series("KOSPI", "테스트", "kr_index", freq, 0, TIER_REQUIRED, sources)


def _close(dates: list[str]) -> pd.Series:
    idx = pd.to_datetime(dates)
    return pd.Series(range(len(dates)), index=idx, dtype="float64")


# ── 신선도 판정 ────────────────────────────────────────────

def test_소스가_18일_고착이면_신선도_미달로_본다():
    s = _close(["2026-09-16", "2026-09-17"])
    assert mds._is_stale(_series(), s, "2026-10-06") is True


def test_연휴_직후_5일_공백은_오탐하지_않는다():
    # 2026 설 연휴: 직전 거래일 2/13 → 2/19 실행(갭 5일). 허용치 7일 안.
    s = _close(["2026-02-12", "2026-02-13"])
    assert mds._is_stale(_series(), s, "2026-02-19") is False
    assert MAX_SOURCE_STALE_DAYS >= 5


def test_백필처럼_end가_과거면_오탐하지_않는다():
    s = _close(["2024-03-28", "2024-03-29"])
    assert mds._is_stale(_series(), s, "2024-03-31") is False


def test_저빈도_지표는_신선도_판정에서_제외된다():
    s = _close(["2026-06-30"])  # 분기 발표 — 당연히 과거
    assert mds._is_stale(_series(freq=FREQ_MONTHLY), s, "2026-10-06") is False


# ── 폴백 동작 ─────────────────────────────────────────────

def test_첫_소스가_고착이면_다음_소스로_넘어간다(monkeypatch):
    fresh = _close(["2026-10-05", "2026-10-06"])
    stale = _close(["2026-09-16", "2026-09-17"])
    calls: list[str] = []

    def fake_fetch(provider, spec, start, end):
        calls.append(provider)
        return stale if provider == "fdr" else fresh

    monkeypatch.setattr(mds, "fetch_price_series", fake_fetch)
    out = mds._fetch_one_series(
        _series(sources=(("fdr", "KS11"), ("naver", "KOSPI"))), "2026-09-01", "2026-10-06"
    )
    assert calls == ["fdr", "naver"]
    assert out is not None and out.index.max() == pd.Timestamp("2026-10-06")


def test_모든_소스가_고착이면_결측으로_둔다(monkeypatch):
    stale = _close(["2026-09-16", "2026-09-17"])
    monkeypatch.setattr(mds, "fetch_price_series", lambda *a, **k: stale)
    out = mds._fetch_one_series(
        _series(sources=(("fdr", "KS11"), ("naver", "KOSPI"))), "2026-09-01", "2026-10-06"
    )
    assert out is None  # ffill 로 덮이지 않도록 비워 둔다 → 파이프라인 1단계가 중단


# ── 네이버 파서 ────────────────────────────────────────────

class _FakeResp:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        return None


_NAVER_BODY = (
    "[['날짜','시가','고가','저가','종가','거래량','외국인소진율'],\n"
    '["20261002", 6950.1, 7010.0, 6940.0, 7003.74, 500000, 0.0],\n'
    '["20261006", 6960.0, 6990.0, 6900.0, 6979.93, 480000, 0.0]]'
)


def test_네이버_EUC_KR_응답을_읽는다(monkeypatch):
    monkeypatch.setattr(ps.requests, "get",
                        lambda *a, **k: _FakeResp(_NAVER_BODY.encode("euc-kr")))
    s = ps.naver_index_close("KOSPI", "2026-10-01", "2026-10-06")
    assert len(s) == 2
    assert s.index.max() == pd.Timestamp("2026-10-06")
    assert s.iloc[-1] == pytest.approx(6979.93)


def test_네이버_응답에_None이_섞여도_죽지_않는다(monkeypatch):
    body = (
        "[['날짜','시가','고가','저가','종가','거래량','외국인소진율'],\n"
        '["20261002", 6950.1, 7010.0, 6940.0, 7003.74, 500000, 0.0],\n'
        '["20261005", None, None, None, None, None, None]]'
    )
    monkeypatch.setattr(ps.requests, "get",
                        lambda *a, **k: _FakeResp(body.encode("euc-kr")))
    s = ps.naver_index_close("KOSPI", "2026-10-01", "2026-10-06")
    assert len(s) == 1  # None 행만 건너뛰고 나머지는 살린다
    assert s.iloc[-1] == pytest.approx(7003.74)


def test_네이버_본문이_비면_예외를_올려_폴백시킨다(monkeypatch):
    monkeypatch.setattr(ps.requests, "get", lambda *a, **k: _FakeResp(b"[]"))
    with pytest.raises(RuntimeError):
        ps.naver_index_close("KOSPI", "2026-10-01", "2026-10-06")
