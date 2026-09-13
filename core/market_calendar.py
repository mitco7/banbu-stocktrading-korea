"""KRX 휴장일 캘린더 + 거래일/장시간 판정 (수동 관리).

무인 스케줄러가 휴장일에 매수/매도 잡을 돌리지 않도록 하는 게이트(§2.4)이자,
time-stop 등 '영업일' 계산에서 한국 공휴일을 반영하는 단일 소스.

관리 규칙:
- KRX_HOLIDAYS 는 **연 1회(연말) KRX 공지 기준으로 수동 갱신**한다. 자동 갱신 코드 금지.
- 임시공휴일 등 연중 공지는 .env 의 KRX_HOLIDAYS_EXTRA(쉼표 구분)로 즉시 반영.
- 잘못 등록된 휴장일은 '그날 거래 스킵'(보수적 실패)이고, 누락된 휴장일은 KIS 주문
  거부(무해)로 끝난다 — 어느 쪽도 중복 주문 같은 사고로는 이어지지 않는다.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from core.config import settings

KST = ZoneInfo("Asia/Seoul")

# 정규장 시간(KST). 서머타임 없음.
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)

# ── KRX 휴장일(주말 제외 평일 휴장만 기재. 연 1회 KRX 공지로 수동 갱신) ──
# ⚠ 2026년 대체공휴일(광복절·추석 연휴 등)은 정부 공고에 따라 달라질 수 있음 —
#   연초 KRX 휴장일 공지( krx.co.kr )와 대조해 확정할 것.
KRX_HOLIDAYS: frozenset[str] = frozenset({
    # 2026
    "2026-01-01",  # 신정
    "2026-02-16", "2026-02-17", "2026-02-18",  # 설날 연휴
    "2026-03-02",  # 삼일절(3/1 일요일) 대체공휴일
    "2026-05-05",  # 어린이날
    "2026-05-25",  # 석가탄신일(5/24 일요일) 대체공휴일
    "2026-06-03",  # 전국동시지방선거
    "2026-08-17",  # 광복절(8/15 토요일) 대체공휴일 — 공고 확인 필요
    "2026-09-24", "2026-09-25",  # 추석 연휴(추석 9/25 금)
    "2026-09-28",  # 추석 연휴 대체공휴일(9/26 토 겹침) — 공고 확인 필요
    "2026-10-05",  # 개천절(10/3 토요일) 대체공휴일
    "2026-10-09",  # 한글날
    "2026-12-25",  # 성탄절
    "2026-12-31",  # 연말 KRX 휴장
    # 2027 (연초분만 — 연말에 전체 갱신)
    "2027-01-01",  # 신정
})


def _all_holidays() -> set[str]:
    """고정 휴장일 + .env 추가 휴장일(KRX_HOLIDAYS_EXTRA) 합집합."""
    extra = {
        d.strip() for d in settings.KRX_HOLIDAYS_EXTRA.split(",") if d.strip()
    }
    return set(KRX_HOLIDAYS) | extra


def _to_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def is_trading_day(value: str | date | datetime) -> bool:
    """해당 KST 날짜가 KRX 거래일인지(주말·휴장일 제외)."""
    d = _to_date(value)
    if d.weekday() >= 5:  # 토(5)/일(6)
        return False
    return d.isoformat() not in _all_holidays()


def is_market_hours(now: datetime | None = None) -> bool:
    """지금이 거래일의 정규장 시간(09:00~15:30 KST)인지."""
    now = now or datetime.now(KST)
    if not is_trading_day(now):
        return False
    return MARKET_OPEN <= now.timetz().replace(tzinfo=None) <= MARKET_CLOSE


def krx_busdays(start: str | date, end: str | date) -> int:
    """[start, end) 구간의 KRX 영업일 수 — 주말 + 휴장일 제외.

    np.busday_count 시맨틱(end 미포함)을 유지해 기존 _busdays 호출과 호환.
    """
    holidays = [np.datetime64(d) for d in sorted(_all_holidays())]
    return int(np.busday_count(_to_date(start), _to_date(end), holidays=holidays))
