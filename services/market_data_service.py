"""시장 데이터 수집·저장 비즈니스 로직 (Step 2).

흐름:
  collect_market_data(start, end)
    → 거시/시장 시계열 + 25종목 종가/거래량 수집(소스 폴백)
    → 영업일 spine 에 outer-join 정렬
    → 결측은 **ffill 만**(bfill 금지): 거시=발표일까지 ffill, 일간가격=연속 3일 한도
    → 와이드 DataFrame(index=date) 반환
  save_market_data(df)
    → market_data 테이블에 멱등 저장(빈 컬럼만 채움)

⚠ look-ahead: 해외 시장 컬럼의 t-1 시프트와 거시 발표일 정렬은 ML 피처 빌더 책임이다.
   본 수집기는 원본을 네이티브 날짜로 적재한다(상세: core.market_spec). 거래량은 ffill 하지 않는다
   (휴장일 거래량을 전일치로 채우면 유동성을 날조하게 됨).
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from core.config import settings
from core.market_spec import (
    ALL_DATA_COLUMNS,
    DATE_COL,
    FREQ_DAILY,
    FREQ_MONTHLY,
    FREQ_QUARTERLY,
    FREQ_WEEKLY,
    MACRO_MARKET_SERIES,
    STOCK_CLOSE_COLS,
    STOCK_CODES,
    STOCK_VOLUME_COLS,
    WARN_FFILL_RUN_DAYS,
    Series,
    stock_close_col,
    stock_volume_col,
)

# 저빈도 지표: 발표 시차를 한 주기 보수 시프트하고, 구간 시작 경계를 채우려 조회를 앞당긴다.
_SHIFT_FREQS = (FREQ_WEEKLY, FREQ_MONTHLY, FREQ_QUARTERLY)
_SLOW_FETCH_LOOKBACK = pd.DateOffset(days=200)
# 일간/종목: 구간 시작이 주말/공휴일일 때 선두 행을 직전 거래일 값으로 채우기 위한 버퍼
# (구간 시작 전 거래일을 함께 수집 → ffill 후 실제 구간으로 잘라냄). KR 최장 연휴 커버.
_LEADING_LOOKBACK = pd.DateOffset(days=20)

# 증분 수집 관련.
KST = ZoneInfo("Asia/Seoul")
_MARKET_OPEN = dtime(9, 0)
_MARKET_CLOSE = dtime(15, 30)
# 증분 자동수집 허용 최대 일수. 초과 시 force 필요 — 초기적재/RLS 빈응답으로 인한 대량 재수집 사고 방지.
_MAX_INCREMENTAL_DAYS = 45
from db.repositories import market_data_repo
from services.collectors.ecos_collector import fetch_ecos_series
from services.collectors.fred_collector import fetch_fred_series
from services.collectors.price_sources import fetch_price_series
from services.collectors.stock_collector import fetch_stock
from utils.dataframe import ffill_grid
from utils.logger import get_logger

logger = get_logger(__name__)


def _apply_publication_shift(s: pd.Series, freq: str) -> pd.Series:
    """저빈도 지표를 발표 시차만큼 보수적으로 한 주기 뒤로 시프트(look-ahead 방지).

    값의 '기준 기간'이 아니라 '실제로 알 수 있게 된 시점'에 노출되도록 한다.
    """
    if freq == FREQ_MONTHLY:
        out = s.copy()
        out.index = out.index + pd.offsets.MonthBegin(1)
        return out
    if freq == FREQ_QUARTERLY:
        out = s.copy()
        out.index = out.index + pd.offsets.QuarterBegin(startingMonth=1)
        return out
    if freq == FREQ_WEEKLY:
        out = s.copy()
        out.index = out.index + pd.Timedelta(days=7)
        return out
    return s


def _fetch_one_series(series: Series, start: str, end: str) -> pd.Series | None:
    """Series 스펙의 소스를 순서대로 시도해 첫 성공 결과 반환."""
    for provider, spec in series.sources:
        try:
            if provider in ("fdr", "yfinance", "pykrx"):
                s = fetch_price_series(provider, spec, start, end)
            elif provider == "ecos":
                s = fetch_ecos_series(spec, start, end)
            elif provider == "fred":
                s = fetch_fred_series(spec, start, end)
            else:
                continue
            if s is not None and not s.empty:
                logger.info("수집 OK: %s ← %s", series.key, provider)
                return s
        except Exception as exc:  # noqa: BLE001 — 폴백 목적
            logger.warning("소스 실패 %s/%s: %s", series.key, provider, exc)
    logger.warning("모든 소스 실패(결측 유지): %s", series.key)
    return None


def collect_market_data(start: str, end: str) -> pd.DataFrame:
    """[start, end] 구간 시장데이터 와이드 DataFrame 수집.

    index = 달력일(date, 주말 포함), columns = ALL_DATA_COLUMNS. 결측은 ffill 규칙대로 처리됨.
    주말/공휴일은 직전 거래일 값으로 ffill 연장된다(정답지 방식). 구간 시작이 주말/공휴일이어도
    선두 행이 비지 않도록, 일간/종목을 구간 시작 전 버퍼까지 수집해 채운 뒤 실제 구간으로 잘라낸다.
    """
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    if end_ts < start_ts:
        raise ValueError(f"빈 날짜 구간: {start} ~ {end}")
    # 확장 그리드(선두 채움용 버퍼 포함). 마지막에 [start, end] 로 잘라낸다.
    ext_start = (start_ts - _LEADING_LOOKBACK).strftime("%Y-%m-%d")
    spine = pd.date_range(start=ext_start, end=end_ts, freq="D")
    frame = pd.DataFrame(index=spine, dtype="float64")

    daily_market_cols: list[str] = []
    slow_cols: list[str] = []

    # ── 거시/시장 시계열 ──────────────────────────────────
    for series in MACRO_MARKET_SERIES:
        # 저빈도(주/월/분기)는 발표 시차 시프트 + 경계 채움용 200일 버퍼,
        # 일간/비정기는 선두 채움용 버퍼(ext_start)부터 수집.
        if series.freq in _SHIFT_FREQS:
            fetch_start = (start_ts - _SLOW_FETCH_LOOKBACK).strftime("%Y-%m-%d")
        else:
            fetch_start = ext_start
        s = _fetch_one_series(series, fetch_start, end)
        if s is not None:
            s = _apply_publication_shift(s, series.freq)
        if s is None:
            frame[series.key] = np.nan
        elif series.freq == FREQ_DAILY:
            # 일간: 거래일 값을 달력일 그리드에 정렬(이후 ffill_grid 로 주말/공휴일·선두 연장).
            frame[series.key] = s.reindex(spine).astype("float64")
        else:
            # 저빈도(주/월/분기/비정기): 직전 값을 끌어와 채운다(method="ffill").
            frame[series.key] = s.reindex(spine, method="ffill").astype("float64")
        (daily_market_cols if series.freq == FREQ_DAILY else slow_cols).append(
            series.key
        )

    # ── 25종목 종가/거래량 ────────────────────────────────
    failed_stocks: list[str] = []
    for code in STOCK_CODES:
        df = fetch_stock(code, ext_start, end)
        if df is None:
            frame[stock_close_col(code)] = np.nan
            frame[stock_volume_col(code)] = np.nan
            failed_stocks.append(code)
        else:
            frame[stock_close_col(code)] = df["close"].reindex(spine).astype("float64")
            frame[stock_volume_col(code)] = (
                pd.to_numeric(df["volume"], errors="coerce").reindex(spine).astype("float64")
            )
    if failed_stocks:
        logger.warning("종목 수집 실패: %s", ", ".join(failed_stocks))

    # ── 결측 처리 (ffill 전용, 달력일 그리드 연속 채움) ──────
    # 저빈도(거시)는 위에서 reindex(method="ffill")로 이미 연속 채워짐.
    # 일간 가격·종목 종가·거래량: 주말/공휴일·선두를 직전 거래일 값으로 연속 ffill.
    #   (버퍼 구간의 직전 거래일에서 끌어오므로 구간 첫 주말도 채워진다.)
    fill_cols = daily_market_cols + STOCK_CLOSE_COLS + STOCK_VOLUME_COLS
    frame, suspicious = ffill_grid(frame, fill_cols, WARN_FFILL_RUN_DAYS)
    if suspicious:
        logger.warning(
            "의심 데이터 갭(연속 %d일 초과 ffill — 정상 연휴 아님): %s",
            WARN_FFILL_RUN_DAYS, suspicious,
        )

    # 버퍼 구간 제거 → 실제 [start, end] 만 반환.
    frame = frame.loc[start_ts:end_ts]
    frame = frame.reindex(columns=ALL_DATA_COLUMNS).astype("float64")
    frame.index.name = DATE_COL
    return frame


def dataframe_to_records(df: pd.DataFrame) -> list[dict]:
    """와이드 DataFrame → Supabase 저장용 레코드 리스트(NaN→None)."""
    records: list[dict] = []
    for ts, row in df.iterrows():
        rec: dict = {DATE_COL: pd.Timestamp(ts).strftime("%Y-%m-%d")}
        for col, val in row.items():
            rec[col] = None if pd.isna(val) else float(val)
        records.append(rec)
    return records


def save_market_data(df: pd.DataFrame) -> dict[str, int]:
    """수집 DataFrame 을 market_data 테이블에 멱등 저장."""
    records = dataframe_to_records(df)
    result = market_data_repo.upsert_fill_empty(records)
    logger.info("저장 결과: upserted=%d", result["upserted"])
    return result


def collect_and_save(start: str, end: str) -> dict[str, int]:
    """수집 + 저장 일괄."""
    df = collect_market_data(start, end)
    return save_market_data(df)


# ──────────────────────────────────────────────────────────────────────────
#  증분 수집 (매일 자동)
# ──────────────────────────────────────────────────────────────────────────
# 행 '수집 완료' 판정용 핵심 컬럼 — **FDR 로 안정적으로 채워지는 것만** 본다.
# 글로벌/yfinance 의존 컬럼(SOX·달러인덱스 등)이 일시적 429 로 비어도 재수집 루프에 빠지지
# 않도록, 항상 채워지는 KR 지수 + 25종목 종가만으로 '그 날짜가 수집되었는지'를 판정한다.
# (선택 컬럼의 일시 결측은 멱등 upsert 가 다음 성공 수집 때 COALESCE 로 채운다.)
_REQUIRED_FILLED_COLS = ["KOSPI", "KOSDAQ"] + STOCK_CLOSE_COLS


def is_market_open(now: datetime | None = None) -> bool:
    """현재 KST 가 한국 정규장(평일 09:00~15:30)인지."""
    now = now or datetime.now(KST)
    if now.weekday() >= 5:  # 토(5)/일(6)
        return False
    return _MARKET_OPEN <= now.timetz().replace(tzinfo=None) <= _MARKET_CLOSE


def get_last_updated_date(fallback_start: str | None = None) -> tuple[str, bool]:
    """다음 증분 수집 시작일과 '빈 DB 여부'를 반환한다.

    - 빈 DB(0행) → (fallback_start, True).
      ⚠ RLS 가 켜져 정책이 없으면 데이터가 있어도 0행이 와서 '과거부터 재수집' 사고가 난다.
        본 테이블은 sql/002 로 RLS 비활성화 + 서비스키 사용을 전제하므로 0행=진짜 빈 DB 로 본다.
        그래도 안전을 위해 (1) 여기서 경고 로그를 남기고, (2) update_market_data 가
        과도한 범위는 force 없이는 수집하지 않는다(이중 안전장치).
    - 마지막 행이 미완성(필수 컬럼에 빈 값) → (마지막날짜, False)  [그 날짜부터 재수집].
    - 마지막 행이 완성 → (마지막날짜+1, False).
    """
    fallback_start = fallback_start or settings.BACKFILL_START_DATE
    rows = market_data_repo.count_rows()
    if rows == 0:
        logger.warning(
            "market_data 0행. (RLS 차단이 아닌 '진짜 빈 DB'인지 확인 — 서비스키+RLS비활성화 전제) "
            "폴백 시작일=%s. 초기 적재는 scripts/backfill.py 권장.",
            fallback_start,
        )
        return fallback_start, True

    last = market_data_repo.fetch_last_row()
    if last is None:  # count>0 인데 행이 안 옴 → RLS 등 이상. 폴백 금지(사고 방지).
        raise RuntimeError(
            "count>0 이지만 마지막 행 조회가 비었습니다. RLS/권한 이상 의심 — 과거 재수집을 막기 위해 중단."
        )
    last_date = str(last[DATE_COL])
    missing = [c for c in _REQUIRED_FILLED_COLS if last.get(c) is None]
    if missing:
        logger.info(
            "마지막 행(%s) 미완성: 빈 필수컬럼 %d개(예: %s) → 그 날짜부터 재수집",
            last_date, len(missing), ", ".join(missing[:3]),
        )
        return last_date, False
    next_date = (pd.Timestamp(last_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    return next_date, False


def update_market_data(force: bool = False) -> dict:
    """마지막 날짜+1(또는 미완성 마지막날짜) ~ 어제까지 증분 수집·저장.

    - 장중(KST 평일 09:00~15:30)에는 당일 종가가 미확정이므로 당일을 포함하지 않는다(어제까지).
      force=True 면 당일까지 포함.
    - 멱등: 여러 번 돌려도 중복 없음(빈 컬럼만 채우는 upsert + 달력일 ffill).
    - 안전장치: 증분 범위가 과도하면(초기적재/RLS 빈응답 의심) force 없이는 수집하지 않는다.
    """
    now = datetime.now(KST)
    today = now.date()
    yesterday = today - timedelta(days=1)
    end_date = today if force else yesterday
    if is_market_open(now) and not force:
        logger.info("장중(KST %s) — 당일 종가 미확정으로 당일 제외, 어제까지만 수집", now.strftime("%H:%M"))

    start_date, is_empty = get_last_updated_date()
    end_str = end_date.strftime("%Y-%m-%d")

    if start_date > end_str:
        logger.info("최신 상태 — 신규 수집 날짜 없음 (start=%s > end=%s)", start_date, end_str)
        return {"status": "up_to_date", "start": start_date, "end": end_str, "upserted": 0}

    span_days = (pd.Timestamp(end_str) - pd.Timestamp(start_date)).days
    if span_days > _MAX_INCREMENTAL_DAYS and not force:
        msg = (
            f"증분 범위가 {span_days}일로 과도(start={start_date}~{end_str}). "
            f"초기 적재는 scripts/backfill.py, 의도적 대량수집은 force=True 사용. "
            f"(빈 DB 여부={is_empty} — RLS 빈응답일 수 있어 자동 대량수집 차단)"
        )
        logger.warning(msg)
        return {"status": "range_too_large", "start": start_date, "end": end_str,
                "span_days": span_days, "upserted": 0, "detail": msg}

    logger.info("증분 수집: %s ~ %s (force=%s)", start_date, end_str, force)
    result = collect_and_save(start_date, end_str)
    return {"status": "collected", "start": start_date, "end": end_str,
            "upserted": result["upserted"]}
