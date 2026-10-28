"""휴장일 판정 검증 — 잘못된 휴장일 1건이 그날 파이프라인·매수를 통째로 건너뛴다.

2026-09-28 사고: 추석 연휴(9/24~9/26) 마지막 날이 토요일인데 대체공휴일로 잘못 등록해
정상 개장일에 파이프라인이 스킵됐다. 설날·추석 연휴는 **일요일**과 겹칠 때만 대체공휴일.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core import market_calendar as mc

KST = ZoneInfo("Asia/Seoul")


def _day(date_str: str, hhmm: str = "10:00") -> datetime:
    return datetime.fromisoformat(f"{date_str}T{hhmm}").replace(tzinfo=KST)


@pytest.mark.parametrize("date_str", [
    "2026-09-28",  # 추석 연휴 후 첫 거래일(9/26 토 겹침 → 대체공휴일 없음)
    "2026-09-23",  # 추석 연휴 직전 거래일
    "2026-08-14",  # 광복절 전날(금)
])
def test_trading_days(date_str):
    assert mc.is_trading_day(_day(date_str)), f"{date_str} 는 개장일이어야 한다"


@pytest.mark.parametrize("date_str", [
    "2026-09-24", "2026-09-25",  # 추석 연휴
    "2026-08-17",                # 광복절(8/15 토) 대체공휴일 — 국경일은 토요일도 대체
    "2026-10-05",                # 개천절(10/3 토) 대체공휴일
    "2026-12-31",                # 연말 KRX 휴장
    "2026-09-26",                # 토요일
])
def test_non_trading_days(date_str):
    assert not mc.is_trading_day(_day(date_str)), f"{date_str} 는 휴장일이어야 한다"


def test_market_hours_boundaries():
    assert mc.is_market_hours(_day("2026-09-28", "09:00"))
    assert mc.is_market_hours(_day("2026-09-28", "15:30"))
    assert not mc.is_market_hours(_day("2026-09-28", "08:59"))
    assert not mc.is_market_hours(_day("2026-09-28", "15:31"))
    assert not mc.is_market_hours(_day("2026-09-25", "10:00"))  # 휴장일은 장중이어도 False


def test_trading_day_range_excludes_weekend_and_chuseok():
    """영업일 spine 에 토·일·추석 휴장이 없어야 한다(market_data 날짜 축)."""
    idx = mc.trading_day_range("2026-09-23", "2026-09-29")
    days = {d.strftime("%Y-%m-%d") for d in idx}
    assert "2026-09-23" in days  # 수(거래일)
    assert "2026-09-28" in days  # 월(거래일)
    assert "2026-09-29" in days  # 화
    assert "2026-09-24" not in days  # 추석
    assert "2026-09-25" not in days
    assert "2026-09-26" not in days  # 토
    assert "2026-09-27" not in days  # 일


def test_holidays_cover_backfill_era():
    """백필 시작(2010)부터 현재 운용연도까지 평일 휴장이 등록돼 있어야 한다."""
    assert "2010-01-01" in mc.KRX_HOLIDAYS
    assert "2011-02-03" in mc.KRX_HOLIDAYS  # 설날(mondovisione 2011 공지와 일치)
    assert "2024-05-01" in mc.KRX_HOLIDAYS  # 근로자의 날
    assert "2026-05-01" in mc.KRX_HOLIDAYS
    assert "2026-06-03" in mc.KRX_HOLIDAYS  # 지방선거(수동 보강)
    assert "2027-01-01" in mc.KRX_HOLIDAYS
    assert len(mc.KRX_HOLIDAYS) >= 250


def test_2010_january_trading_days_exclude_new_year():
    idx = mc.trading_day_range("2010-01-01", "2010-01-05")
    days = [d.strftime("%Y-%m-%d") for d in idx]
    assert "2010-01-01" not in days  # 신정(금)
    assert "2010-01-04" in days  # 월
    assert "2010-01-05" in days
