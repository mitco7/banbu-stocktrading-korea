"""가격형 시계열 소스 제공자 (FDR / yfinance / pykrx).

각 함수는 종가(또는 지수값) pd.Series 를 반환한다(index=tz-naive 일자, normalize).
거래대금/거래량이 필요한 종목 수집은 stock_collector 가 별도로 처리한다.
pykrx 는 본 개발환경에서 KRX 접속이 막혀 있어 실패할 수 있다 → 폴백으로 대응.
"""

from __future__ import annotations

import contextlib
import io
import os
import time

import pandas as pd

from utils.logger import get_logger
from utils.retry import retry

logger = get_logger(__name__)

# yfinance(야후) rate-limit(429) 완화: 호출 간 최소 간격을 둔다(정답지도 time.sleep 사용).
_MIN_YF_INTERVAL = 1.2
_last_yf_call = [0.0]


def _yf_throttle() -> None:
    now = time.monotonic()
    wait = _MIN_YF_INTERVAL - (now - _last_yf_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_yf_call[0] = time.monotonic()


def _reset_yf_cookie_cache() -> None:
    """야후가 crumb 발급에서 한 번 429를 주면 yfinance 는 그 깨진 쿠키/crumb 를
    인메모리 싱글톤(YfData)과 디스크(cookies.db)에 영구 캐시해 두고 계속 재사용한다.
    → 재기동해도 모든 요청이 429 로 막히는 자가복구 불능 상태가 된다.
    이를 끊기 위해 인메모리 쿠키/crumb 를 비우고 디스크 쿠키 DB 를 삭제한다.
    (tkr-tz.db 시간대 캐시는 정상 동작하므로 건드리지 않는다.)"""
    # 1) 인메모리 싱글톤의 쿠키/crumb 무효화 (장기 실행 프로세스에서 필수)
    try:
        from yfinance import data as _yd

        inst = _yd.YfData._instances.get(_yd.YfData)
        if inst is not None:
            inst._cookie = None
            inst._crumb = None
    except Exception as exc:  # 내부 구조 변동에 대비 — 실패해도 디스크 삭제는 진행
        logger.debug("yfinance 인메모리 쿠키 리셋 실패(무시): %s", exc)

    # 2) 디스크 cookies.db 삭제 (현재 프로세스 연결을 먼저 닫아 잠금 해제)
    try:
        from yfinance import cache as _yc

        mgr = _yc._CookieDBManager
        cache_dir = mgr.get_location()
        mgr.close_db()
        mgr._db = None
        _yc._CookieCacheManager._Cookie_cache = None  # 매니저 싱글톤 재초기화 유도
        for name in ("cookies.db", "cookies.db-shm", "cookies.db-wal"):
            path = os.path.join(cache_dir, name)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as exc:
                logger.debug("쿠키 캐시 파일 삭제 실패 %s: %s", path, exc)
        logger.info("yfinance 쿠키 캐시 리셋 완료: %s", cache_dir)
    except Exception as exc:
        logger.debug("yfinance 쿠키 캐시 리셋 실패(무시): %s", exc)


def _yf_download(ticker: str, start: str, end: str) -> pd.DataFrame:
    """yf.download 래퍼. 빈/429 응답이면 오염된 쿠키 캐시를 리셋하고 1회 재시도한다.
    (yf end 는 exclusive → +1일 보정)"""
    import yfinance as yf

    end_excl = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    _yf_throttle()
    df = yf.download(
        ticker, start=start, end=end_excl, progress=False, auto_adjust=True
    )
    if df is None or df.empty:
        # 야후 rate-limit 으로 쿠키 캐시가 오염됐을 수 있음 → 리셋 후 1회 재시도
        logger.warning("yfinance 빈 응답(%s) → 쿠키 캐시 리셋 후 재시도", ticker)
        _reset_yf_cookie_cache()
        _yf_throttle()
        df = yf.download(
            ticker, start=start, end=end_excl, progress=False, auto_adjust=True
        )
    if df is None or df.empty:
        raise RuntimeError(f"yfinance 빈 응답: {ticker}")
    return df


def _normalize_index(s: pd.Series) -> pd.Series:
    s = s.copy()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")]
    return s.sort_index()


@retry(attempts=2, base_delay=2.0, backoff=2.0)
def fdr_close(ticker: str, start: str, end: str) -> pd.Series:
    """FinanceDataReader 종가 시계열. (FDR end 는 inclusive)"""
    import FinanceDataReader as fdr

    df = fdr.DataReader(ticker, start, end)
    if df is None or df.empty or "Close" not in df.columns:
        raise RuntimeError(f"FDR 빈 응답: {ticker}")
    return _normalize_index(df["Close"].astype("float64"))


@retry(attempts=1, base_delay=5.0, backoff=2.0)
def yfinance_close(ticker: str, start: str, end: str) -> pd.Series:
    """yfinance 수정종가 시계열. (오염된 쿠키 캐시 시 자동 리셋·재시도)"""
    df = _yf_download(ticker, start, end)
    close = df["Close"]
    if isinstance(close, pd.DataFrame):  # 단일 티커도 MultiIndex 로 올 때가 있음
        close = close.iloc[:, 0]
    return _normalize_index(close.astype("float64"))


@retry(attempts=1, base_delay=5.0, backoff=2.0)
def yfinance_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    """yfinance 수정 OHLCV → DataFrame[close, volume] (종목 폴백용). (자동 리셋·재시도)"""
    df = _yf_download(ticker, start, end)
    close = df["Close"]
    volume = df["Volume"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    if isinstance(volume, pd.DataFrame):
        volume = volume.iloc[:, 0]
    out = pd.DataFrame({
        "close": pd.to_numeric(close, errors="coerce").astype("float64"),
        "volume": pd.to_numeric(volume, errors="coerce").astype("float64"),
    })
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    return out[~out.index.duplicated(keep="last")].sort_index()


@retry(attempts=2, base_delay=2.0, backoff=2.0)
def pykrx_index(symbol: str, start: str, end: str) -> pd.Series:
    """pykrx 지수 시계열(VKOSPI 등). 본 환경에서 KRX 차단 시 예외 발생.

    pykrx 는 KRX 접속 실패 시 print/잘못된 logging 으로 콘솔을 더럽히므로,
    호출 동안 stdout/stderr 를 삼킨다(예외는 그대로 전파되어 폴백/갭 처리됨).
    VKOSPI 코드 매핑은 pykrx 버전에 따라 다를 수 있어 best-effort 다.
    """
    from pykrx import stock

    code_map = {"VKOSPI": "1#VKOSPI"}  # 표준 코드 부재 → 시도용. 실패 시 폴백/갭.
    code = code_map.get(symbol, symbol)
    s_date, e_date = start.replace("-", ""), end.replace("-", "")
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        df = stock.get_index_ohlcv_by_date(s_date, e_date, code)
    if df is None or df.empty or "종가" not in df.columns:
        raise RuntimeError(f"pykrx 빈 응답: {symbol}")
    return _normalize_index(df["종가"].astype("float64"))


def fetch_price_series(
    provider: str, spec, start: str, end: str
) -> pd.Series:
    """provider 디스패치. 실패 시 예외를 그대로 올려 호출자가 다음 폴백을 시도."""
    if provider == "fdr":
        return fdr_close(spec, start, end)
    if provider == "yfinance":
        return yfinance_close(spec, start, end)
    if provider == "pykrx":
        return pykrx_index(spec, start, end)
    raise ValueError(f"가격 소스가 아님: {provider}")
