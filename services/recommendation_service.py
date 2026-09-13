"""매수 후보 생성 (Step 4): ML 1차 후보 + 기술적 지표 + 수급 결합 → stock_recommendations 저장.

흐름:
  generate_recommendations(ref_date, min_rise_pct)
    1) stock_analysis_results 에서 ML 예측 상승률(predicted_rise_pct) >= 임계값(기본 3%) 인 종목을 1차 후보로.
       (컬럼명은 "Rise Probability (%)" 지만 실제는 (예측가-현재가)/현재가×100 = 상승률, 확률 아님·§2.2)
    2) 각 후보의 FDR 일봉 OHLCV(거래일만)로 SMA/RSI/MACD/ATR/ADX/거래량비율 계산.
    3) investor_flow 최신 스냅샷의 flow_score(-1~+1, 감성점수 자리) 결합.
    4) 날짜(추천 산출일) + 모든 지표/근거를 stock_recommendations 에 멱등 저장.

⚠ 종합점수 가중합·LLM 검토는 다음 스텝(여기선 지표 계산 + 후보 저장까지만 — 요구사항 [하지 말 것]).
⚠ 지표는 market_data 가 아닌 FDR 일봉으로 계산(market_data 는 종가만/주말 ffill → ATR/ADX 불가·잡음).
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from core.config import settings
from core.universe import NAME_TO_CODE
from db.repositories import analysis_repo, flow_repo, recommendation_repo
from services import indicators as ind
from services.collectors.stock_collector import fetch_stock_ohlcv
from utils.logger import get_logger

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")

# stock_analysis_results 의 ML 예측 상승률 컬럼(이름은 'Probability'이나 실제는 상승률 %).
_ML_RISE_COL = "Rise Probability (%)"
_ML_STOCK_COL = "Stock"
_ML_LASTPRICE_COL = "Last Actual Price"
_ML_PREDPRICE_COL = "Predicted Future Price"
_ML_PREDRISE_COL = "Predicted Rise"
_ML_RECO_COL = "Recommendation"


def _f(value: object, ndigits: int | None = None) -> float | None:
    """NaN/None/inf → None, 그 외 float(선택적 반올림). DB(JSON) 안전 직렬화용."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, ndigits) if ndigits is not None else f


def _last(series: pd.Series) -> float | None:
    """시계열의 마지막 유효값(없으면 None)."""
    if series is None or series.empty:
        return None
    s = series.dropna()
    return float(s.iloc[-1]) if not s.empty else None


def _compute_indicators(ohlcv: pd.DataFrame) -> dict:
    """일봉 OHLCV(거래일만) → 최근 시점 지표 값 dict."""
    close, high, low, volume = ohlcv["close"], ohlcv["high"], ohlcv["low"], ohlcv["volume"]

    sma_s = _last(ind.sma(close, settings.SMA_SHORT))
    sma_l = _last(ind.sma(close, settings.SMA_LONG))
    rsi = _last(ind.rsi_wilder(close, settings.RSI_PERIOD))
    macd_line, signal_line, hist = ind.macd(
        close, settings.MACD_FAST, settings.MACD_SLOW, settings.MACD_SIGNAL
    )
    macd_v, sig_v, hist_v = _last(macd_line), _last(signal_line), _last(hist)
    atr = _last(ind.atr_wilder(high, low, close, settings.ATR_PERIOD))
    adx_s, plus_s, minus_s = ind.adx_wilder(high, low, close, settings.ADX_PERIOD)
    adx, plus_di, minus_di = _last(adx_s), _last(plus_s), _last(minus_s)
    vol_ratio = _last(ind.volume_ratio(volume, settings.VOLUME_RATIO_WINDOW))
    close_price = _last(close)

    golden_cross = (sma_s is not None and sma_l is not None and sma_s > sma_l)
    macd_buy = (macd_v is not None and sig_v is not None and macd_v > sig_v)
    trend_strong = (adx is not None and adx >= settings.ADX_TREND_MIN)
    rsi_signal = None
    if rsi is not None:
        if rsi >= settings.RSI_OVERBOUGHT:
            rsi_signal = "overbought"
        elif rsi <= settings.RSI_OVERSOLD:
            rsi_signal = "oversold"
        else:
            rsi_signal = "neutral"
    atr_pct = (atr / close_price * 100.0) if (atr and close_price) else None

    return {
        "close_price": _f(close_price, 4),
        "sma_short": _f(sma_s, 4),
        "sma_long": _f(sma_l, 4),
        "golden_cross": golden_cross,
        "rsi": _f(rsi, 4),
        "rsi_signal": rsi_signal,
        "macd": _f(macd_v, 6),
        "macd_signal": _f(sig_v, 6),
        "macd_hist": _f(hist_v, 6),
        "macd_buy": macd_buy,
        "atr": _f(atr, 4),
        "atr_pct": _f(atr_pct, 4),
        "adx": _f(adx, 4),
        "plus_di": _f(plus_di, 4),
        "minus_di": _f(minus_di, 4),
        "trend_strong": trend_strong,
        "volume_ratio": _f(vol_ratio, 4),
        "indicator_date": ohlcv.index[-1].strftime("%Y-%m-%d"),
    }


def _fetch_ohlcv_safe(code: str, start: str, end: str) -> pd.DataFrame | None:
    """FDR 일봉 조회를 wall-clock 타임아웃으로 감싼다(FDR 는 timeout 미노출 → 무한대기 방지).

    초과/예외 시 None(해당 종목만 제외, 전체 작업은 계속).
    """
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(fetch_stock_ohlcv, code, start, end).result(
                timeout=settings.RECOMMEND_FDR_TIMEOUT_SEC
            )
    except FuturesTimeout:
        logger.warning("FDR 조회 타임아웃(%.0fs) %s — 제외", settings.RECOMMEND_FDR_TIMEOUT_SEC, code)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("FDR 조회 실패 %s: %s — 제외", code, exc)
        return None


def _ml_is_stale(rows: list[dict], rec_date: str) -> bool:
    """ML 분석 결과가 rec_date 기준 허용 신선도를 넘겼는지(stale) 판정.

    created_at(UTC) 최댓값을 KST 날짜로 환산해 rec_date 와의 일수 차이를 본다.
    stale 이면 오래된 예측으로 매수 후보를 만들지 않도록(fail-close) True.
    """
    stamps = [pd.Timestamp(r["created_at"]) for r in rows if r.get("created_at")]
    if not stamps:
        logger.warning("ML 결과에 created_at 없음 — 신선도 확인 불가, stale 로 간주(fail-close).")
        return True
    latest_kst = max(stamps).tz_convert(KST).normalize()
    age_days = (pd.Timestamp(rec_date, tz=KST).normalize() - latest_kst).days
    if age_days > settings.RECOMMEND_ML_MAX_AGE_DAYS:
        logger.warning(
            "ML 분석 결과가 오래됨: 최신 산출=%s, 기준일=%s (%d일 초과) → 후보 생성 중단(fail-close).",
            latest_kst.date(), rec_date, age_days,
        )
        return True
    return False


def _null_indicators() -> dict:
    """지표 계산 불가(데이터 부족) 시 채울 NULL 템플릿. _compute_indicators 와 키 집합 동일(멱등)."""
    keys = (
        "close_price", "sma_short", "sma_long", "golden_cross", "rsi", "rsi_signal",
        "macd", "macd_signal", "macd_hist", "macd_buy", "atr", "atr_pct",
        "adx", "plus_di", "minus_di", "trend_strong", "volume_ratio", "indicator_date",
    )
    return {k: None for k in keys}


def _select_universe_rows(rows: list[dict]) -> list[dict]:
    """ML 분석 결과 중 유니버스 내 + 예측 상승률 유효한 전 종목(상승률 내림차순).

    후보 여부(is_candidate)와 무관하게 25종목 전부를 저장하기 위함. 후보 필터는 호출부에서 적용.
    """
    universe = [
        r for r in rows
        if r.get(_ML_STOCK_COL) in NAME_TO_CODE and r.get(_ML_RISE_COL) is not None
    ]
    universe.sort(key=lambda r: float(r.get(_ML_RISE_COL) or -1e9), reverse=True)
    return universe


def generate_recommendations(
    ref_date: str | None = None, min_rise_pct: float | None = None
) -> dict:
    """매수 후보 생성·저장. ref_date 없으면 오늘(KST)을 추천 산출일로 사용."""
    min_rise = settings.RECOMMEND_MIN_RISE_PCT if min_rise_pct is None else min_rise_pct
    rec_date = ref_date or datetime.now(KST).strftime("%Y-%m-%d")
    hist_start = (
        pd.Timestamp(rec_date) - pd.Timedelta(days=settings.RECOMMEND_HISTORY_DAYS)
    ).strftime("%Y-%m-%d")

    analysis_rows = analysis_repo.fetch_latest_analysis()
    if not analysis_rows:
        logger.warning("stock_analysis_results 비어있음 — ML 예측 없음. 후보 생성 중단.")
        return {"date": rec_date, "candidates": 0, "stored": 0, "min_rise_pct": min_rise,
                "status": "no_ml_data"}
    if _ml_is_stale(analysis_rows, rec_date):
        return {"date": rec_date, "candidates": 0, "stored": 0, "min_rise_pct": min_rise,
                "status": "stale_ml"}

    universe_rows = _select_universe_rows(analysis_rows)
    if not universe_rows:
        logger.warning("ML 결과에 유니버스 종목이 없음 — 저장할 데이터 없음.")
        return {"date": rec_date, "candidates": 0, "stored": 0, "min_rise_pct": min_rise}

    # 수급 최신 스냅샷(ticker → row).
    flow_by_ticker = {r["ticker"]: r for r in flow_repo.fetch_latest_snapshot()}

    records: list[dict] = []
    no_indicators: list[str] = []   # 일봉 부족으로 지표 NULL 처리된 종목(행은 저장됨)
    candidate_count = 0
    for row in universe_rows:
        name = row[_ML_STOCK_COL]
        code = NAME_TO_CODE[name]
        rise_pct = float(row[_ML_RISE_COL])
        is_candidate = rise_pct >= min_rise
        if is_candidate:
            candidate_count += 1

        rec = {
            "날짜": rec_date,
            "ticker": code,
            "name": name,
            # ── ML 근거 ──
            "last_actual_price": _f(row.get(_ML_LASTPRICE_COL), 4),
            "predicted_future_price": _f(row.get(_ML_PREDPRICE_COL), 4),
            "predicted_rise_pct": _f(rise_pct, 6),
            "predicted_rise": bool(row.get(_ML_PREDRISE_COL)) if row.get(_ML_PREDRISE_COL) is not None else None,
            "ml_recommendation": row.get(_ML_RECO_COL),
            # ── 후보 여부 / 메타 ──
            "is_candidate": is_candidate,       # 예측 상승률 >= 임계값(매수 후보)인지
            "rise_threshold": _f(min_rise, 4),
        }

        # ── 기술적 지표(후보 여부와 무관하게 전 종목 계산) ──
        ohlcv = _fetch_ohlcv_safe(code, hist_start, rec_date)
        if ohlcv is None or len(ohlcv) < settings.RECOMMEND_MIN_BARS:
            logger.warning(
                "%s(%s): 일봉 데이터 부족(%s봉, 최소 %d) → 지표 NULL(행은 저장)",
                name, code, "None" if ohlcv is None else len(ohlcv), settings.RECOMMEND_MIN_BARS,
            )
            rec.update(_null_indicators())
            no_indicators.append(name)
        else:
            rec.update(_compute_indicators(ohlcv))
            if rec["indicator_date"] != rec_date:
                logger.debug(
                    "%s(%s): 지표 기준 마지막 거래일=%s (산출일 %s 과 다름 — 당일 봉 미확정/휴장)",
                    name, code, rec["indicator_date"], rec_date,
                )

        # ── 수급 결합(없으면 4컬럼 모두 NULL — 멱등 키 통일) ──
        flow = flow_by_ticker.get(code)
        rec["flow_score"] = _f(flow.get("flow_score"), 6) if flow else None
        rec["foreign_net"] = _f(flow.get("foreign_net"), 2) if flow else None
        rec["institution_net"] = _f(flow.get("institution_net"), 2) if flow else None
        rec["flow_calc_date"] = flow.get("calculation_date") if flow else None

        records.append(rec)

    stored = recommendation_repo.replace_for_date(rec_date, records)
    logger.info(
        "종목 분석 저장: %d종목 (날짜=%s, 매수후보=%d, 지표NULL=%d, 임계=%.2f%%)",
        stored, rec_date, candidate_count, len(no_indicators), min_rise,
    )
    return {
        "date": rec_date,
        "stored": stored,
        "candidates": candidate_count,
        "no_indicators": no_indicators,
        "min_rise_pct": min_rise,
    }


def list_for_date(date: str | None = None) -> dict:
    """특정 날짜(기본 최근 산출일)의 매수 후보 목록 조회."""
    target = date or recommendation_repo.latest_date()
    if target is None:
        return {"date": None, "count": 0, "recommendations": []}
    rows = recommendation_repo.fetch_by_date(target)
    return {"date": target, "count": len(rows), "recommendations": rows}
