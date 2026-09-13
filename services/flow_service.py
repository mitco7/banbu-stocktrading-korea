"""외국인·기관 수급(순매수) 점수화 (뉴스감성 대체) — Step 3 일부.

흐름:
  collect_and_store_flows(lookback_days)
    → 25종목 각각 최근 N거래일 외국인 순매수 + 기관 순매수(금액) 합산(pykrx)
    → 거래대금 대비 비율로 정규화 → flow_score(-1~+1) (양수=매수우위=호재성)
    → investor_flow 테이블에 종목별 스냅샷 upsert(매 실행 갱신)

점수 정규화(CLAUDE.md §2.2 — 종목 자체 분포 기준, 단일일 절대금액 금지):
    flow_raw = 외국인 순매수 + 기관 순매수            (N거래일 합)
    ratio    = flow_raw / 거래대금(N거래일 합)         (그 종목의 거래활동 대비 — 종목 자체 스케일)
    flow_score = tanh(ratio / FLOW_SCORE_SCALE)        (-1~+1 로 squash, 부호 보존)

데이터 소스(FLOW_SOURCE):
  - "naver"(기본): 네이버 금융 종목별 외국인/기관 일별 순매매(주식수)×종가로 금액 환산. **로그인 불필요.**
  - "pykrx": KRX 투자자별 순매수 — KRX 로그인(KRX_ID/KRX_PW) 필요(본 환경 차단).
  - "auto": naver 실패 시 pykrx 폴백.
  점수 산출 로직(compute_flow_score)은 소스와 무관한 순수 함수다.
"""

from __future__ import annotations

import contextlib
import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from core.config import settings
from core.universe import KR_UNIVERSE
from db.repositories import flow_repo
from utils.logger import get_logger
from utils.retry import retry

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")

# pykrx 투자자 구분 컬럼명 후보(버전에 따라 다름).
_FOREIGN_COLS = ("외국인합계", "외국인")
_INSTITUTION_COLS = ("기관합계", "기관")

# 네이버 금융(로그인 불필요) — 종목별 외국인/기관 일별 순매매.
_NAVER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_NAVER_FRGN_URL = "https://finance.naver.com/item/frgn.naver?code={code}&page={page}"


def compute_flow_score(
    foreign_net: float, institution_net: float, trade_value: float, scale: float | None = None
) -> float:
    """수급 점수(-1~+1). 순매수 합을 거래대금 대비 비율로 정규화 후 tanh squash.

    Args:
        foreign_net: 외국인 순매수 합(원).
        institution_net: 기관 순매수 합(원).
        trade_value: 같은 기간 거래대금 합(원). 0 이하/결측이면 0.0 반환.
        scale: tanh 스케일(기본 settings.FLOW_SCORE_SCALE).
    Returns:
        -1.0 ~ +1.0 (양수=매수우위=호재성).
    """
    scale = scale if scale is not None else settings.FLOW_SCORE_SCALE
    if trade_value is None or not np.isfinite(trade_value) or trade_value <= 0:
        return 0.0
    flow_raw = (foreign_net or 0.0) + (institution_net or 0.0)
    ratio = flow_raw / trade_value
    return float(np.tanh(ratio / scale))


def _pick_col(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


@retry(attempts=2, base_delay=1.5, backoff=2.0)
def _naver_get(url: str, code: str) -> str:
    r = requests.get(
        url,
        headers={"User-Agent": _NAVER_UA,
                 "Referer": f"https://finance.naver.com/item/frgn.naver?code={code}"},
        timeout=10,
    )
    r.raise_for_status()
    r.encoding = "euc-kr"
    return r.text


def _parse_naver_frgn(html: str) -> pd.DataFrame | None:
    """네이버 외국인/기관 일별 표 파싱 → date/close/volume/inst_sh/foreign_sh(순매매 주식수).

    표는 9컬럼(날짜·종가·전일비·등락률·거래량·기관 순매매량·외국인 순매매량·보유주수·보유율).
    위치 기준으로 추출하고, 날짜가 유효한 표만 반환.
    """
    for t in pd.read_html(io.StringIO(html)):
        if t.shape[1] < 9:
            continue
        out = pd.DataFrame({
            "date": pd.to_datetime(t.iloc[:, 0], format="%Y.%m.%d", errors="coerce"),
            "close": pd.to_numeric(t.iloc[:, 1], errors="coerce"),
            "volume": pd.to_numeric(t.iloc[:, 4], errors="coerce"),
            "inst_sh": pd.to_numeric(t.iloc[:, 5], errors="coerce"),
            "foreign_sh": pd.to_numeric(t.iloc[:, 6], errors="coerce"),
        }).dropna(subset=["date", "close", "volume"])
        if not out.empty:
            return out
    return None


def _fetch_flow_naver(
    code: str, lookback: int
) -> tuple[float, float, float, str] | None:
    """네이버 금융으로 (외국인 순매수액, 기관 순매수액, 거래대금, 최근거래일) 반환. 빈/부족 시 None.

    순매매량(주)×종가로 금액 환산(가격이 분자·분모에서 상쇄돼 비율은 정확). 로그인 불필요.
    """
    pages = max(1, lookback // 8 + 1)
    frames: list[pd.DataFrame] = []
    for page in range(1, pages + 1):
        try:
            html = _naver_get(_NAVER_FRGN_URL.format(code=code, page=page), code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("네이버 수급 요청 실패 %s p%d: %s", code, page, exc)
            break
        parsed = _parse_naver_frgn(html)
        if parsed is None or parsed.empty:
            break
        frames.append(parsed)
        if sum(len(f) for f in frames) >= lookback:
            break

    if not frames:
        return None
    df = (
        pd.concat(frames)
        .drop_duplicates(subset=["date"])
        .sort_values("date")
        .reset_index(drop=True)
    )
    if len(df) < lookback:  # 거래일 부족(상장 직후 등) → fail-close
        return None

    tail = df.tail(lookback)
    foreign_net = float((tail["foreign_sh"] * tail["close"]).sum())
    institution_net = float((tail["inst_sh"] * tail["close"]).sum())
    trade_value = float((tail["volume"] * tail["close"]).sum())
    last_date = tail["date"].max().strftime("%Y-%m-%d")
    return foreign_net, institution_net, trade_value, last_date


@retry(attempts=2, base_delay=2.0, backoff=2.0)
def _fetch_flow_pykrx(
    code: str, fromdate: str, todate: str, lookback: int
) -> tuple[float, float, float] | None:
    """pykrx 로 (외국인 순매수합, 기관 순매수합, 거래대금합) 반환. 빈 응답이면 None.

    pykrx 는 KRX 차단 시 print/잘못된 logging 으로 콘솔을 더럽히므로 stdout/stderr 를 삼킨다.
    """
    from pykrx import stock

    s_date, e_date = fromdate.replace("-", ""), todate.replace("-", "")
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        value_df = stock.get_market_trading_value_by_date(s_date, e_date, code)
        ohlcv = stock.get_market_ohlcv_by_date(s_date, e_date, code)

    if value_df is None or value_df.empty or ohlcv is None or ohlcv.empty:
        return None

    fcol = _pick_col(value_df, _FOREIGN_COLS)
    icol = _pick_col(value_df, _INSTITUTION_COLS)
    if fcol is None or icol is None or "거래대금" not in ohlcv.columns:
        logger.warning("종목 %s: 예상 컬럼 없음(value=%s, ohlcv=%s)",
                       code, list(value_df.columns), list(ohlcv.columns))
        return None

    # 거래일 수 부족(상장 직후·거래정지 복귀 등) → fail-close 로 제외.
    if len(value_df) < lookback or len(ohlcv) < lookback:
        logger.info("종목 %s: 거래일 부족(value=%d, ohlcv=%d < %d) → 제외",
                    code, len(value_df), len(ohlcv), lookback)
        return None

    # 최근 lookback 거래일만 합산.
    foreign_net = float(value_df[fcol].tail(lookback).sum())
    institution_net = float(value_df[icol].tail(lookback).sum())
    trade_value = float(ohlcv["거래대금"].tail(lookback).sum())
    return foreign_net, institution_net, trade_value


def _fetch_flow(
    code: str, lookback: int, fromdate: str, todate: str
) -> tuple[float, float, float, str] | None:
    """소스(FLOW_SOURCE)에 따라 (외국인 순매수, 기관 순매수, 거래대금, 최근거래일) 반환.

    naver(로그인 불필요, 기본) / pykrx(KRX 로그인 필요) / auto(naver→pykrx 폴백).
    """
    src = settings.FLOW_SOURCE
    if src in ("naver", "auto"):
        res = _fetch_flow_naver(code, lookback)
        if res is not None:
            return res
        if src == "naver":
            return None
    res_pk = _fetch_flow_pykrx(code, fromdate, todate, lookback)
    if res_pk is None:
        return None
    foreign_net, institution_net, trade_value = res_pk
    return foreign_net, institution_net, trade_value, todate


def collect_investor_flows(
    lookback_days: int | None = None, ref_date: str | None = None
) -> list[dict]:
    """25종목의 수급 점수 레코드 리스트 산출(저장은 하지 않음)."""
    lookback = lookback_days or settings.FLOW_LOOKBACK_DAYS
    # 기준일: 지정 없으면 어제(KST). 당일은 장중 미확정.
    if ref_date is None:
        ref_date = (datetime.now(KST).date() - timedelta(days=1)).strftime("%Y-%m-%d")
    # 거래일 lookback 개를 확보하려 달력일 버퍼를 넉넉히 둔다(연휴 포함).
    fromdate = (pd.Timestamp(ref_date) - pd.Timedelta(days=lookback * 2 + 14)).strftime("%Y-%m-%d")

    records: list[dict] = []
    failed: list[str] = []
    for code, name, _market in KR_UNIVERSE:
        try:
            res = _fetch_flow(code, lookback, fromdate, ref_date)
        except Exception as exc:  # noqa: BLE001 — 소스 차단 등
            logger.warning("수급 수집 실패 %s: %s", code, exc)
            res = None
        if res is None:
            failed.append(code)
            continue
        foreign_net, institution_net, trade_value, last_date = res
        # 유동성 필터(fail-close): 거래대금 미달 시 분모가 작아 점수가 과대 증폭 → 제외.
        if trade_value < settings.FLOW_MIN_TRADE_VALUE:
            logger.info("종목 %s: 거래대금 %0.0f < 하한 → 제외(유동성)", code, trade_value)
            failed.append(code)
            continue
        score = compute_flow_score(foreign_net, institution_net, trade_value)
        records.append({
            "ticker": code,
            "name": name,
            "foreign_net": foreign_net,
            "institution_net": institution_net,
            "flow_score": round(score, 6),
            "lookback_days": lookback,
            "calculation_date": last_date,
        })

    if failed:
        logger.warning(
            "수급 수집 실패/제외 %d/%d 종목(소스=%s): %s",
            len(failed), len(KR_UNIVERSE), settings.FLOW_SOURCE,
            ", ".join(failed[:5]) + ("..." if len(failed) > 5 else ""),
        )
    return records


def collect_and_store_flows(
    lookback_days: int | None = None, ref_date: str | None = None
) -> dict:
    """수급 점수 산출 + investor_flow 스냅샷 저장."""
    records = collect_investor_flows(lookback_days, ref_date)
    if not records:
        logger.warning(
            "수급 데이터 0건 — 소스(%s) 접속/파싱 실패. investor_flow 미갱신.",
            settings.FLOW_SOURCE,
        )
        return {"stored": 0, "lookback_days": lookback_days or settings.FLOW_LOOKBACK_DAYS}
    stored = flow_repo.upsert_flows(records)
    logger.info("수급 점수 저장: %d종목 (lookback=%d일)", stored, records[0]["lookback_days"])
    return {"stored": stored, "lookback_days": records[0]["lookback_days"],
            "calculation_date": records[0]["calculation_date"]}
