"""한국 주식 호가단위(tick) 처리 — 순수 함수 (Step 6).

지정가 주문은 호가단위에 맞아야 거부되지 않는다. 상·하한가(±30%)도 호가단위로 맞춘다.
호가단위 표는 core.config.HOGA_TICKS 로 외부화(매직넘버 금지, §2.3).
"""

from __future__ import annotations

import math

from core.config import HOGA_TICKS, settings


def tick_size(price: float) -> int:
    """가격대에 해당하는 호가단위(원)."""
    for upper, tick in HOGA_TICKS:
        if price < upper:
            return tick
    return HOGA_TICKS[-1][1]


def round_to_tick(price: float, mode: str = "nearest") -> int:
    """가격을 호가단위에 맞춰 정수 원으로 조정. mode: nearest/down/up."""
    if price <= 0:
        return 0
    tick = tick_size(price)
    q = price / tick
    if mode == "down":
        n = math.floor(q)
    elif mode == "up":
        n = math.ceil(q)
    else:
        n = math.floor(q + 0.5)
    return int(n * tick)


def price_limits(prev_close: float) -> tuple[int, int]:
    """전일 종가 기준 상한가/하한가(±PRICE_LIMIT_PCT)를 호가단위로 맞춰 반환.

    상한가는 내림(초과 방지), 하한가는 올림(미만 방지)으로 범위 안쪽으로 보정.
    """
    pct = settings.PRICE_LIMIT_PCT
    upper = round_to_tick(prev_close * (1 + pct), mode="down")
    lower = round_to_tick(prev_close * (1 - pct), mode="up")
    return upper, lower


def clamp_to_limit(price: float, prev_close: float) -> int:
    """지정가를 호가단위로 맞추고 ±30% 범위로 클램프."""
    upper, lower = price_limits(prev_close)
    p = round_to_tick(price, mode="nearest")
    return max(lower, min(upper, p))


def within_price_limit(price: float, prev_close: float) -> bool:
    """가격이 ±30% 범위 안인지."""
    upper, lower = price_limits(prev_close)
    return lower <= price <= upper
