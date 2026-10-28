"""시장 데이터 수집·저장 비즈니스 로직 (Step 2).

흐름:
  collect_market_data(start, end)
    → 거시/시장 시계열 + 25종목 종가/거래량 수집(소스 폴백)
    → **KRX 영업일 spine** 에 정렬(주말·휴장일 행 없음, CLAUDE.md §2.1)
    → 결측은 **ffill 만**(bfill 금지): 거시=발표일까지 ffill, 일간 종가/지수=연속 3영업일 한도
    → 거래량은 ffill 하지 않음(결측=NULL)
    → 와이드 DataFrame(index=영업일) 반환
  save_market_data(df)
    → market_data 테이블에 멱등 저장(빈 컬럼만 채움)

⚠ look-ahead: 해외 시장 컬럼의 t-1 시프트와 거시 발표일 정렬은 ML 피처 빌더 책임이다.
   본 수집기는 원본을 네이티브 날짜로 적재한다(상세: core.market_spec).
⚠ 기존 DB에 남아 있는 주말·휴장 행은 이 수집기가 삭제하지 않는다(upsert만).
   정리 필요 시 별도 스크립트/SQL 로 비영업일 행을 제거한다.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from core.config import settings
from core.market_calendar import trading_day_range
from core.market_spec import (
    ALL_DATA_COLUMNS,
    DATE_COL,
    FFILL_LIMIT_BUSINESS_DAYS,
    FREQ_DAILY,
    FREQ_MONTHLY,
    FREQ_QUARTERLY,
    FREQ_WEEKLY,
    MACRO_MARKET_SERIES,
    MAX_SOURCE_STALE_DAYS,
    STOCK_CLOSE_COLS,
    STOCK_CODES,
    STOCK_VOLUME_COLS,
    TIER_REQUIRED,
    Series,
    stock_close_col,
    stock_volume_col,
)

# 저빈도 지표: 발표 시차를 한 주기 보수 시프트하고, 구간 시작 경계를 채우려 조회를 앞당긴다.
_SHIFT_FREQS = (FREQ_WEEKLY, FREQ_MONTHLY, FREQ_QUARTERLY)
_SLOW_FETCH_LOOKBACK = pd.DateOffset(days=200)
# 구간 첫 영업일의 소스 결측 시 직전 영업일 값을 ffill 한도 내로 끌어오기 위한 조회 버퍼.
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
from utils.dataframe import ffill_limited
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


_PRICE_PROVIDERS = ("naver", "fdr", "yfinance", "pykrx")


def _is_stale(series: Series, s: pd.Series, end: str) -> bool:
    """일간 소스가 응답은 줬지만 마지막 날짜가 기준일보다 한참 과거인가.

    소스가 조용히 고착되는 사고(2026-10-06 FDR 지수)를 '성공'으로 기록하지 않기 위한 판정.
    주간 이하 저빈도 지표는 발표 시차가 정상이라 제외한다.
    """
    if series.freq != FREQ_DAILY or s.empty:
        return False
    last = pd.Timestamp(s.index.max()).normalize()
    return (pd.Timestamp(end).normalize() - last).days > MAX_SOURCE_STALE_DAYS


def _fetch_one_series(series: Series, start: str, end: str) -> pd.Series | None:
    """Series 스펙의 소스를 순서대로 시도해 첫 '신선한' 결과 반환."""
    stale_seen = False
    for provider, spec in series.sources:
        try:
            if provider in _PRICE_PROVIDERS:
                s = fetch_price_series(provider, spec, start, end)
            elif provider == "ecos":
                s = fetch_ecos_series(spec, start, end)
            elif provider == "fred":
                s = fetch_fred_series(spec, start, end)
            else:
                continue
            if s is not None and not s.empty:
                # 가격형 소스에만 적용 — FRED/ECOS 거시지표는 발표 지연이 정상이라 제외.
                if provider in _PRICE_PROVIDERS and _is_stale(series, s, end):
                    stale_seen = True
                    logger.warning(
                        "소스 신선도 미달 %s/%s: 마지막 %s (기준 %s, 허용 %d일) → 다음 소스 시도",
                        series.key, provider, pd.Timestamp(s.index.max()).date(),
                        end, MAX_SOURCE_STALE_DAYS,
                    )
                    continue
                logger.info("수집 OK: %s ← %s", series.key, provider)
                return s
        except Exception as exc:  # noqa: BLE001 — 폴백 목적
            logger.warning("소스 실패 %s/%s: %s", series.key, provider, exc)
    if stale_seen and series.tier == TIER_REQUIRED:
        # 응답은 오지만 전부 과거 데이터 — ffill 로 덮이면 조용히 썩는다. 필수 지표는 에러로.
        logger.error(
            "필수 지표 수집 실패 — 모든 소스가 %d일 넘게 과거 데이터만 반환: %s (소스 점검 필요)",
            MAX_SOURCE_STALE_DAYS, series.key,
        )
    else:
        logger.warning("모든 소스 실패(결측 유지): %s", series.key)
    return None


def collect_market_data(start: str, end: str,
                        missing_out: list[str] | None = None) -> pd.DataFrame:
    """[start, end] 구간 시장데이터 와이드 DataFrame 수집.

    index = **KRX 영업일**(주말·휴장 제외), columns = ALL_DATA_COLUMNS.
    결측은 영업일 그리드 위 ffill 규칙(일간 종가/지수: 최대 FFILL_LIMIT_BUSINESS_DAYS,
    거래량: ffill 안 함, 거시: 한도 없이 발표일 유지).
    구간 첫 영업일의 소스 결측을 한도 내로 메우려 선두 버퍼 영업일까지 수집한 뒤
    실제 [start, end] 영업일만 잘라 반환한다.

    missing_out: 리스트를 주면 '필수(TIER_REQUIRED) 일간 지표인데 모든 소스가 실패/신선도 미달'인
      컬럼명을 담아 돌려준다. 호출자(파이프라인)가 fail-close 판단에 쓴다.
      ⚠ 이 결측을 그냥 두면 하류 ML 노트북의 ffill 이 '직전 고착값'으로 메워 버려서,
        수집 단계에서 막지 않으면 오염을 탐지할 수 없다(2026-10-06 FDR 지수 고착 사고).
    """
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    if end_ts < start_ts:
        raise ValueError(f"빈 날짜 구간: {start} ~ {end}")
    # 확장 영업일 그리드(선두 채움용 버퍼 포함). 마지막에 [start, end] 영업일로 잘라낸다.
    ext_start = (start_ts - _LEADING_LOOKBACK).strftime("%Y-%m-%d")
    spine = trading_day_range(ext_start, end_ts)
    if spine.empty:
        logger.warning("영업일 spine 비어 있음: %s ~ %s", start, end)
        empty = pd.DataFrame(columns=ALL_DATA_COLUMNS, dtype="float64")
        empty.index.name = DATE_COL
        return empty
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
            if (missing_out is not None and series.tier == TIER_REQUIRED
                    and series.freq == FREQ_DAILY):
                missing_out.append(series.key)
            frame[series.key] = np.nan
        elif series.freq == FREQ_DAILY:
            # 일간: 소스 거래일 값을 영업일 spine 에 정렬(이후 한도 ffill).
            frame[series.key] = s.reindex(spine).astype("float64")
        else:
            # 저빈도(주/월/분기/비정기): 직전 값을 영업일에 끌어와 채운다.
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
            # 거래량: reindex 만 — ffill 금지(당일 실측).
            frame[stock_volume_col(code)] = (
                pd.to_numeric(df["volume"], errors="coerce").reindex(spine).astype("float64")
            )
    if failed_stocks:
        logger.warning("종목 수집 실패: %s", ", ".join(failed_stocks))

    # ── 결측 처리 (ffill 전용, 영업일 그리드·한도) ──────
    # 저빈도(거시)는 위에서 reindex(method="ffill")로 이미 연속 채워짐.
    # 일간 지수/환율·종목 종가만 한도 ffill. 거래량은 제외.
    fill_cols = daily_market_cols + STOCK_CLOSE_COLS
    frame, gaps, _filled = ffill_limited(
        frame, fill_cols, limit=FFILL_LIMIT_BUSINESS_DAYS
    )
    if gaps:
        logger.warning(
            "데이터 갭(연속 %d영업일 초과 ffill 한도 — NaN 유지): %s",
            FFILL_LIMIT_BUSINESS_DAYS, gaps,
        )

    # 버퍼 구간 제거 → 실제 [start, end] 영업일만 반환.
    out_index = trading_day_range(start_ts, end_ts)
    frame = frame.reindex(out_index)
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


def collect_and_save(start: str, end: str) -> dict:
    """수집 + 저장 일괄. 필수 일간 지표 결측 목록(required_missing)을 함께 돌려준다."""
    missing: list[str] = []
    df = collect_market_data(start, end, missing_out=missing)
    result = dict(save_market_data(df))
    result["required_missing"] = missing
    return result


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
    - 멱등: 여러 번 돌려도 중복 없음(빈 컬럼만 채우는 upsert + 영업일 ffill).
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
            "upserted": result["upserted"],
            "required_missing": result.get("required_missing", [])}
