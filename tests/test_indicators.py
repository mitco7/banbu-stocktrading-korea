"""기술적 지표 단위 검증 — 손계산 기댓값 + 표준 속성.

목적: Wilder RSI/ATR/ADX, MACD, SMA, 거래량비율 공식이 표준과 일치하는지 회귀 방지.
(stock-quant 에이전트 검증과 별개로 코드 레벨에서 고정.)
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from services import indicators as ind


def _s(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


# ── Wilder RMA ────────────────────────────────────────────────────────────
def test_wilder_rma_seed_and_recursion():
    out = ind.wilder_rma(_s([1, 2, 3, 4, 5]), 3).tolist()
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert out[2] == pytest.approx(2.0)          # 시드 = mean(1,2,3)
    assert out[3] == pytest.approx(8 / 3)        # (2*2+4)/3
    assert out[4] == pytest.approx((8 / 3 * 2 + 5) / 3)


def test_wilder_rma_skips_leading_nan():
    # 선두 NaN(diff/shift 잔재)을 건너뛰고 첫 유효값부터 시드해야 한다.
    out = ind.wilder_rma(_s([np.nan, 1, 1, 0]), 2).tolist()
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert out[2] == pytest.approx(1.0)          # mean(1,1)
    assert out[3] == pytest.approx(0.5)          # (1*1+0)/2


def test_wilder_rma_insufficient_data_all_nan():
    out = ind.wilder_rma(_s([1, 2]), 5)
    assert out.isna().all()


# ── SMA ───────────────────────────────────────────────────────────────────
def test_sma_basic():
    out = ind.sma(_s([1, 2, 3, 4, 5]), 3).tolist()
    assert math.isnan(out[1])
    assert out[2:] == pytest.approx([2.0, 3.0, 4.0])


# ── RSI (Wilder) ──────────────────────────────────────────────────────────
def test_rsi_hand_computed_period2():
    rsi = ind.rsi_wilder(_s([10, 11, 12, 11]), period=2).tolist()
    assert math.isnan(rsi[0]) and math.isnan(rsi[1])
    assert rsi[2] == pytest.approx(100.0)        # 손실 평균 0 → 100
    assert rsi[3] == pytest.approx(50.0)         # 이득=손실 평균 → RS=1 → 50


def test_rsi_all_gains_is_100():
    rsi = ind.rsi_wilder(_s(list(range(1, 31))), period=14)
    assert rsi.dropna().iloc[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_0():
    rsi = ind.rsi_wilder(_s(list(range(30, 0, -1))), period=14)
    assert rsi.dropna().iloc[-1] == pytest.approx(0.0)


def test_rsi_bounded_0_100():
    rng = np.random.default_rng(0)
    close = _s(np.cumsum(rng.normal(0, 1, 200)) + 100)
    rsi = ind.rsi_wilder(close, 14).dropna()
    assert ((rsi >= 0) & (rsi <= 100)).all()


# ── MACD ──────────────────────────────────────────────────────────────────
def test_macd_relationships():
    rng = np.random.default_rng(1)
    close = _s(np.cumsum(rng.normal(0, 1, 120)) + 100)
    macd_line, signal, hist = ind.macd(close, 12, 26, 9)
    # hist = macd - signal 항등.
    assert (hist.dropna() - (macd_line - signal).dropna()).abs().max() == pytest.approx(0.0, abs=1e-9)
    # 일정 상승 추세에서 MACD>0.
    up = _s(list(range(1, 80)))
    m, s, h = ind.macd(up)
    assert m.iloc[-1] > 0


# ── True Range / ATR ──────────────────────────────────────────────────────
def test_atr_constant_range():
    high = _s([10, 11, 12])
    low = _s([8, 9, 10])
    close = _s([9, 10, 11])
    atr = ind.atr_wilder(high, low, close, period=2).tolist()
    assert math.isnan(atr[0])
    assert atr[1] == pytest.approx(2.0)
    assert atr[2] == pytest.approx(2.0)


def test_true_range_uses_prev_close_gap():
    # 갭 상승: 저가가 전일 종가보다 높으면 TR = 고-전일종가.
    high = _s([10, 20])
    low = _s([9, 18])
    close = _s([9.5, 19])
    tr = ind.true_range(high, low, close).tolist()
    assert tr[1] == pytest.approx(20 - 9.5)      # |고 - 전일종가| = 10.5 > (고-저=2)


# ── ADX ───────────────────────────────────────────────────────────────────
def test_adx_uptrend_plus_di_dominates():
    n = 60
    high = _s([100 + i for i in range(n)])
    low = _s([98 + i for i in range(n)])
    close = _s([99 + i for i in range(n)])
    adx, plus_di, minus_di = ind.adx_wilder(high, low, close, 14)
    assert plus_di.dropna().iloc[-1] > minus_di.dropna().iloc[-1]
    a = adx.dropna()
    assert ((a >= 0) & (a <= 100)).all()
    assert a.iloc[-1] > 25  # 뚜렷한 추세 → 강한 ADX


def test_adx_downtrend_minus_di_dominates():
    n = 60
    high = _s([200 - i for i in range(n)])
    low = _s([198 - i for i in range(n)])
    close = _s([199 - i for i in range(n)])
    _adx, plus_di, minus_di = ind.adx_wilder(high, low, close, 14)
    assert minus_di.dropna().iloc[-1] > plus_di.dropna().iloc[-1]


# ── 거래량비율 ─────────────────────────────────────────────────────────────
def test_volume_ratio_basic():
    vol = _s([100, 100, 100, 100, 100, 200])
    vr = ind.volume_ratio(vol, window=5)
    assert vr.iloc[5] == pytest.approx(2.0)      # 200 / mean(직전5=100)


def test_volume_ratio_filters_zero():
    # 0 거래량(휴장 잔재)은 평균에서 제외 → 분모 왜곡 없음.
    vol = _s([100, 0, 100, 100, 100, 100, 150])
    vr = ind.volume_ratio(vol, window=5)
    assert vr.iloc[6] is not None and math.isfinite(vr.iloc[6])
