"""25종목 일봉 수집 — 수정주가 종가 + 거래량.

소스: FDR(수정주가; split-adjusted Close) 기본 → 실패 시 **yfinance 폴백**('<코드>.KS'/'.KQ').
  (FDR 미설치/실패 환경에서도 종목이 비지 않도록. 거시/지수와 동일한 폴백 정책.)
반환: DataFrame(index=일자, columns=['close','volume']). 실패 시 None.
상장 시점이 다른 종목은 초기 구간이 비어 있을 수 있다(정상 — 결측 허용).
"""

from __future__ import annotations

import pandas as pd

from core.universe import CODE_TO_MARKET
from services.collectors.price_sources import yfinance_ohlcv
from utils.logger import get_logger
from utils.retry import retry

logger = get_logger(__name__)


@retry(attempts=2, base_delay=2.0, backoff=2.0)
def _fdr_ohlcv(code: str, start: str, end: str) -> pd.DataFrame:
    import FinanceDataReader as fdr

    df = fdr.DataReader(code, start, end)
    if df is None or df.empty:
        raise RuntimeError(f"FDR 빈 응답: {code}")
    return df


def _normalize(close, volume) -> pd.DataFrame:
    out = pd.DataFrame({
        "close": pd.to_numeric(close, errors="coerce").astype("float64"),
        "volume": (pd.to_numeric(volume, errors="coerce").astype("float64")
                   if volume is not None else float("nan")),
    })
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    return out[~out.index.duplicated(keep="last")].sort_index()


def fetch_stock_ohlcv(code: str, start: str, end: str) -> pd.DataFrame | None:
    """단일 종목 일봉 OHLCV (고·저가 포함, **거래일만**). 기술적 지표(ATR/ADX) 계산용.

    FDR 은 실제 거래일만 반환(주말·공휴일·ffill 더미 없음)하고 High/Low 를 제공하므로
    per-종목 지표 입력으로 적합하다. 반환: DataFrame[open,high,low,close,volume] (index=일자).
    실패 시 None(yfinance 폴백은 고·저가를 안 줘 ATR/ADX 불가 → 여기선 FDR 전용).
    """
    try:
        df = _fdr_ohlcv(code, start, end)
    except Exception as exc:  # noqa: BLE001
        logger.warning("종목 OHLCV FDR 실패 %s: %s", code, exc)
        return None
    needed = ("Open", "High", "Low", "Close")
    if any(col not in df.columns for col in needed):
        logger.warning("종목 %s: OHLC 컬럼 누락(%s)", code, list(df.columns))
        return None
    out = pd.DataFrame({
        "open": pd.to_numeric(df["Open"], errors="coerce").astype("float64"),
        "high": pd.to_numeric(df["High"], errors="coerce").astype("float64"),
        "low": pd.to_numeric(df["Low"], errors="coerce").astype("float64"),
        "close": pd.to_numeric(df["Close"], errors="coerce").astype("float64"),
        "volume": (pd.to_numeric(df["Volume"], errors="coerce").astype("float64")
                   if "Volume" in df.columns else float("nan")),
    })
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out if not out.empty else None


def fetch_stock(code: str, start: str, end: str) -> pd.DataFrame | None:
    """단일 종목의 종가/거래량. FDR → yfinance 폴백. 둘 다 실패 시 None."""
    # 1) FDR 수정주가(기본).
    try:
        df = _fdr_ohlcv(code, start, end)
        if df.get("Close") is not None:
            return _normalize(df["Close"], df.get("Volume"))
    except Exception as exc:  # noqa: BLE001 — 폴백 목적
        logger.warning("종목 FDR 실패 %s: %s — yfinance 폴백", code, exc)

    # 2) yfinance 폴백 (KOSPI='.KS', KOSDAQ='.KQ').
    suffix = ".KQ" if CODE_TO_MARKET.get(code) == "KOSDAQ" else ".KS"
    try:
        out = yfinance_ohlcv(f"{code}{suffix}", start, end)
        if out is not None and not out.empty:
            logger.info("수집 OK: %s ← yfinance", code)
            return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("종목 yfinance 폴백 실패 %s: %s", code, exc)

    logger.warning("종목 수집 실패(FDR/yfinance 모두): %s", code)
    return None
