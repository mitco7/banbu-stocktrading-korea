"""기술적 지표 — 순수 함수 (Step 4: 매수 후보용).

입력은 **실제 거래일만** 인덱싱된 일봉 시계열이어야 한다(주말·공휴일·ffill 더미 제외).
  - market_data 는 영업일 축·종가 위주라 ATR/ADX(고·저가 필요)에 부적합하다.
  - 호출자(recommendation_service)는 FDR 일봉 OHLCV(거래일만)를 입력으로 준다.
    이로써 "주말/공휴일 제외"와 RSI 의 "ffill 변동0 날 제외"가 입력 단계에서 자연히 충족된다.

평활(Wilder, RSI/ATR/ADX): Wilder 의 RMA(=alpha 1/n 지수평활, 단 표준 시딩).
  RMA_n = mean(x_1..x_n) 으로 시드한 뒤 RMA_t = (RMA_{t-1}*(n-1) + x_t)/n.
  (ewm(alpha=1/n, adjust=False) 는 x_1 부터 시작해 초기 구간이 살짝 다르므로, 외부 차트
   /표준과 더 잘 맞도록 시드 방식을 명시적으로 구현한다.)
MACD 의 EMA 는 span 기반 표준 지수평활(adjust=False).

모든 함수는 부작용 없는 순수 함수다(테스트·검증 용이, CLAUDE.md §2.2 look-ahead 없음 —
현재 시점 값은 과거·현재 관측만 사용).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ──────────────────────────────────────────────────────────────────────────
#  평활 헬퍼
# ──────────────────────────────────────────────────────────────────────────
def wilder_rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder 의 RMA(running moving average). 표준 시딩.

    첫 (period-1) 개는 NaN, period 번째 = 직전 period 개 단순평균, 이후 재귀:
        rma_t = (rma_{t-1} * (period-1) + x_t) / period
    RSI/ATR/ADX 의 Wilder 평활에 공통 사용.
    """
    if period <= 0:
        raise ValueError("period 는 1 이상이어야 합니다.")
    x = series.astype("float64").to_numpy()
    n = len(x)
    out = np.full(n, np.nan, dtype="float64")
    valid = ~np.isnan(x)
    if valid.sum() < period:
        return pd.Series(out, index=series.index)
    # 선두 NaN(diff/shift 로 생기는 첫 칸 등)은 건너뛰고, 첫 유효값부터 period 개로 시드.
    first = int(np.argmax(valid))           # 첫 유효 인덱스
    seed_end = first + period - 1
    if seed_end >= n:
        return pd.Series(out, index=series.index)
    seed = float(np.nanmean(x[first : seed_end + 1]))
    out[seed_end] = seed
    prev = seed
    for i in range(seed_end + 1, n):
        xi = x[i]
        if np.isnan(xi):  # 중간 데이터 갭: 갱신 없이 직전값 유지(보수적). 거래일 입력이면 발생 안 함.
            out[i] = prev
            continue
        prev = (prev * (period - 1) + xi) / period
        out[i] = prev
    return pd.Series(out, index=series.index)


def ema(series: pd.Series, span: int) -> pd.Series:
    """표준 지수이동평균(span 기반, adjust=False). MACD 용."""
    return series.astype("float64").ewm(span=span, adjust=False).mean()


# ──────────────────────────────────────────────────────────────────────────
#  추세 / 모멘텀
# ──────────────────────────────────────────────────────────────────────────
def sma(series: pd.Series, period: int) -> pd.Series:
    """단순이동평균."""
    return series.astype("float64").rolling(window=period, min_periods=period).mean()


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI(Wilder). gain/loss 를 Wilder RMA 로 평활.

    delta=0 인 날(상승·하락 힘 모두 0)은 gain/loss 양쪽에 0 으로 들어가 자연 처리된다.
    입력이 거래일만이면(FDR) ffill 더미로 인한 가짜 0 변동일은 애초에 존재하지 않는다.
    avg_loss=0 → RSI=100, avg_gain=0 → RSI=0 으로 정의.
    """
    close = close.astype("float64")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = wilder_rma(gain, period)
    avg_loss = wilder_rma(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)  # 0 나눗셈 경고 회피(아래 where 로 덮음)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # 경계: 손실0(상승만)→100, 이득0(하락만)→0, 양쪽0(완전 보합)→50(중립).
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    rsi = rsi.where(avg_gain != 0.0, 0.0)
    rsi = rsi.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    # 워밍업 구간(평균이 아직 NaN)은 NaN 유지.
    rsi = rsi.where(avg_gain.notna() & avg_loss.notna())
    return rsi


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD = EMA(fast) - EMA(slow), Signal = EMA(signal, MACD), Hist = MACD - Signal.

    Returns: (macd_line, signal_line, histogram). 매수신호 = macd_line > signal_line.
    """
    close = close.astype("float64")
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


# ──────────────────────────────────────────────────────────────────────────
#  변동성 / 추세강도 (High/Low 필요)
# ──────────────────────────────────────────────────────────────────────────
def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True Range = max(고-저, |고-전일종가|, |저-전일종가|)."""
    high = high.astype("float64")
    low = low.astype("float64")
    prev_close = close.astype("float64").shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr


def atr_wilder(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """ATR(Wilder) = True Range 의 Wilder RMA."""
    tr = true_range(high, low, close)
    return wilder_rma(tr, period)


def adx_wilder(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """ADX(Wilder). +DI/-DI → DX → DX 의 Wilder RMA = ADX.

    +DM = up_move  (if up_move > down_move and up_move > 0 else 0)
    -DM = down_move(if down_move > up_move and down_move > 0 else 0)
      where up_move = 고_t - 고_{t-1}, down_move = 저_{t-1} - 저_t
    +DI = 100 * RMA(+DM)/RMA(TR),  -DI = 100 * RMA(-DM)/RMA(TR)
    DX  = 100 * |+DI - -DI| / (+DI + -DI)
    ADX = RMA(DX)

    Returns: (adx, plus_di, minus_di).
    """
    high = high.astype("float64")
    low = low.astype("float64")
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0), index=high.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0),
        index=high.index,
    )
    atr = wilder_rma(true_range(high, low, close), period)
    plus_di = 100.0 * wilder_rma(plus_dm, period) / atr
    minus_di = 100.0 * wilder_rma(minus_dm, period) / atr
    di_sum = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum.replace(0.0, np.nan)
    both_valid = plus_di.notna() & minus_di.notna()
    # 진짜 무방향(둘 다 유효 + 합=0)만 DX=0. 워밍업 구간 NaN 은 0 으로 덮지 않는다
    # (가짜 0 이 ADX 시드 평균에 섞여 추세강도를 낮게 왜곡 → 거짓 trend_strong 방지).
    dx = dx.where(~(both_valid & (di_sum == 0.0)), 0.0)
    dx = dx.where(both_valid)
    adx = wilder_rma(dx, period)
    return adx, plus_di, minus_di


# ──────────────────────────────────────────────────────────────────────────
#  거래량
# ──────────────────────────────────────────────────────────────────────────
def volume_ratio(volume: pd.Series, window: int = 5) -> pd.Series:
    """거래량비율 = 오늘 거래량 / 직전 window 거래일 평균 거래량.

    0/결측 거래량(거래정지·휴장 잔재 등)은 평균 계산에서 제외(NaN 처리)해 왜곡 방지.
    분모(직전 window 일)는 '오늘 제외' 과거만 사용(shift(1)) — look-ahead 없음.
    """
    vol = pd.to_numeric(volume, errors="coerce").astype("float64")
    vol = vol.where(vol > 0.0)  # 0/음수 → NaN(평균에서 제외)
    # 직전 window 거래일 평균. 0 이 섞이면 그 날만 빼고 나머지로 평균(과반 이상 유효할 때만).
    # 실제 거래일(FDR) 입력이면 0 이 없어 항상 정확히 window 일 평균이 된다.
    min_valid = max(1, window // 2 + 1)
    prev_mean = vol.shift(1).rolling(window=window, min_periods=min_valid).mean()
    return vol / prev_mean
