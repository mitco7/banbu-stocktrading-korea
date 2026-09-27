"""종합점수 산정 + 하드필터 + VIX 게이트 + LLM 거부권 오케스트레이션 (Step 5).

설계 원칙(prompt/9):
  - 점수 전 **하드필터(fail-close)**: SELL·수급결측·과매수·이격과열·극단변동성·기술약화·기준일격차 종목 제외.
    점수로 되살리지 않는다. flow_score=None 을 0.5(중립)로 치환 금지(정보없음→중립 미화 = 누수).
  - **풀 크기에 안 흔들리는 절대·유계 가중합**(횡단면 z-score 금지 — 2~5종목 표본은 표준화 무의미).
    각 팩터를 0~1 절대 서브스코어로 변환 후 가중합(가중치 합=1.0, 전부 config).
  - **VIX[T-1] 게이트**(VKOSPI 없음 → VIX 대체, 반드시 shift(1) — 당일 미국 VIX 는 한국 아침에 모름 = 누수).
  - **tech 의 ADX 는 +DI/-DI 방향 가드**(강한 하락추세에 만점 주는 버그 차단), **flow 는 외국인/기관 합치도** 반영.

모든 서브스코어 함수는 순수 함수(테스트·look-ahead 검증 용이). 현재 시점 값은 과거·현재 관측만 사용.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from core import market_calendar
from core.config import SCORE_TIE_BREAKERS, settings
from core.market_spec import DATE_COL
from db.repositories import (
    analysis_repo,
    decision_repo,
    market_data_repo,
    recommendation_repo,
)
from services import llm_review_service
from utils.logger import get_logger

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")
VIX_COL = "VIX 지수"


# ──────────────────────────────────────────────────────────────────────────
#  서브스코어 (순수 함수, 모두 0~1)
# ──────────────────────────────────────────────────────────────────────────
def ml_sub(predicted_rise_pct: float) -> float:
    """ML 모멘텀: 예측 상승률을 [0, ML_RISE_CAP] 로 saturate → 0~1."""
    cap = settings.ML_RISE_CAP
    return float(np.clip(predicted_rise_pct, 0.0, cap) / cap)


def rsi_pt(rsi: float) -> float:
    """RSI 건강도 0~1 (40~75 만점, 딥과매도/과열은 감점). 경계는 config."""
    lo_floor = settings.RSI_OVERSOLD_FLOOR   # 30
    full_lo = settings.RSI_FULL_LO           # 40
    full_hi = settings.RSI_FULL_HI           # 75
    hard_max = settings.RSI_HARD_MAX         # 82
    if rsi <= lo_floor:
        return 0.5
    if rsi < full_lo:                        # 30~40: 0.5 → 1.0 선형
        return 0.5 + 0.5 * (rsi - lo_floor) / (full_lo - lo_floor)
    if rsi <= full_hi:                       # 40~75: 만점
        return 1.0
    if rsi <= hard_max:                      # 75~82: 1.0 → 0.6 선형 감점
        return 1.0 - 0.4 * (rsi - full_hi) / (hard_max - full_hi)
    return 0.0                               # >82: 하드필터에서 이미 제외


def adx_pt(adx: float, plus_di: float, minus_di: float) -> float:
    """ADX 추세세기(0~1) × 방향가드. +DI>-DI(상승)일 때만 세기 인정, 아니면 0.

    ADX 는 세기만 알고 방향은 모른다 → +DI/-DI 안 보면 강한 '하락'추세에 만점 주는 버그.
    (DI 를 별도 점수항으로 더하지 않는다 — gc/macd 가 이미 방향 → 3중 카운트 방지. DI 는 on/off 스위치.)
    """
    direction_ok = 1.0 if (plus_di is not None and minus_di is not None and plus_di > minus_di) else 0.0
    if adx is None:
        return 0.0
    return float(np.clip(adx, 0.0, settings.ADX_CAP) / settings.ADX_CAP) * direction_ok


def tech_sub(
    golden_cross: bool, macd_buy: bool, rsi: float,
    adx: float, plus_di: float, minus_di: float,
) -> float:
    """기술 종합 0~1. gc/macd(실질 추세신호) 무겁게, rsi/adx 보조. 내부가중 합=1.0(config)."""
    gc_pt = 1.0 if golden_cross else 0.0
    macd_pt = 1.0 if macd_buy else 0.0
    rp = rsi_pt(rsi) if rsi is not None else 0.0
    ap = adx_pt(adx, plus_di, minus_di)
    return (
        settings.TECH_W_GC * gc_pt
        + settings.TECH_W_MACD * macd_pt
        + settings.TECH_W_RSI * rp
        + settings.TECH_W_ADX * ap
    )


def _sign(x: float | None) -> int:
    if x is None or x == 0:
        return 0
    return 1 if x > 0 else -1


def flow_signals(foreign_net: float | None, institution_net: float | None) -> dict:
    """외국인/기관 부호로 합치도 판정. agree/disagree/dual_buy 플래그."""
    fs, is_ = _sign(foreign_net), _sign(institution_net)
    return {
        "agree": fs != 0 and is_ != 0 and fs == is_,
        "disagree": fs != 0 and is_ != 0 and fs != is_,
        "dual_buy": (foreign_net or 0) > 0 and (institution_net or 0) > 0,
    }


def flow_sub(flow_score: float, foreign_net: float | None, institution_net: float | None) -> float:
    """수급 0~1. flow_score(-1~+1)→0~1 후, 외국인·기관 이견이면 신뢰도 CONFLUENCE_MULT 배(죽이진 않음)."""
    flow_base = (flow_score + 1.0) / 2.0
    sig = flow_signals(foreign_net, institution_net)
    if sig["disagree"]:
        return flow_base * settings.CONFLUENCE_MULT
    return flow_base


def vol_sub(volume_ratio: float | None) -> float:
    """거래량 0~1. 평소(1.0)=0, VOL_RATIO_CAP 배 이상=1.0(상단 cap — 단발 이벤트 무한가산 방지)."""
    if volume_ratio is None:
        return 0.0
    cap = settings.VOL_RATIO_CAP
    return float(np.clip(volume_ratio - 1.0, 0.0, cap - 1.0) / (cap - 1.0))


def ext20(close_price: float, sma_short: float) -> float | None:
    """이격도 = (종가 - SMA20) / SMA20. 20일선 대비 위로 얼마나 벌어졌나."""
    if close_price is None or sma_short is None or sma_short == 0:
        return None
    return (close_price - sma_short) / sma_short


def total_score(ml: float, tech: float, flow: float, vol: float) -> float:
    """가중합(가중치 합=1.0). 절대·유계 — 풀 크기에 무관."""
    return (
        settings.W_ML * ml + settings.W_TECH * tech
        + settings.W_FLOW * flow + settings.W_VOL * vol
    )


# ──────────────────────────────────────────────────────────────────────────
#  하드필터 (점수 전 제외, fail-close)
# ──────────────────────────────────────────────────────────────────────────
def _busday_gap(dates: list[str]) -> int:
    """날짜 문자열 리스트의 최대 KRX 영업일 격차(주말 + 휴장일 제외).

    ⚠ 휴장일을 빼지 않으면 연휴 뒤 정상 데이터가 stale 로 오판된다 — 2026-09-28(추석 연휴
    직후 첫 거래일)에 직전 거래일(9/23) 데이터의 격차가 3으로 계산돼 후보 4종목이 전부
    asof_gap 으로 탈락했다. 휴장일 제외 시 1영업일. trading_service._busdays 와 동일 기준.
    """
    ds = sorted(pd.Timestamp(d).date() for d in dates if d)
    if len(ds) < 2:
        return 0
    return market_calendar.krx_busdays(ds[0], ds[-1])


def hard_filter(row: dict, effective_min_rise: float, ml_asof: str | None) -> str | None:
    """제외 사유 문자열 반환(통과면 None). 순서대로 첫 위반 사유.

    effective_min_rise: VIX 게이트가 상향한 실효 최소 상승률(정상=3%, 보수=5%).
    ml_asof: ML 예측 기준일(기준일 격차 검사용).
    """
    if (row.get("ml_recommendation") or "").upper() == "SELL":
        return "ml_sell"                       # ML 모순 SELL — ml_sub 만 보고 무시 금지
    if row.get("flow_score") is None:
        return "flow_missing"                  # 수급 결측 — 0.5 치환 금지, 제외
    rsi = row.get("rsi")
    if rsi is None:
        return "no_indicators"                 # 지표 NULL(데이터 부족) — fail-close
    if rsi > settings.RSI_HARD_MAX:
        return "rsi_overheated"
    ex = ext20(row.get("close_price"), row.get("sma_short"))
    if ex is None:
        return "no_ext20"
    if ex > settings.EXT20_MAX:
        return "ext20_overheated"
    atr_pct = row.get("atr_pct")
    if atr_pct is None or atr_pct > settings.ATR_PCT_MAX:
        return "atr_extreme"
    # 기술 사전필터: (골든크로스 / RSI 매수권 / MACD 매수) 중 최소 TECH_PREFILTER_MIN 충족.
    rsi_buy = settings.RSI_PREFILTER_LO <= rsi <= settings.RSI_PREFILTER_HI
    tech_hits = sum([bool(row.get("golden_cross")), rsi_buy, bool(row.get("macd_buy"))])
    if tech_hits < settings.TECH_PREFILTER_MIN:
        return "tech_weak"
    # 기준일 격차: ML as-of / 지표 / 수급 기준일이 임계 초과로 어긋나면 fail-close.
    gap = _busday_gap([ml_asof, row.get("indicator_date"), row.get("flow_calc_date")])
    if gap > settings.ASOF_MAX_GAP_DAYS:
        return f"asof_gap({gap}d)"
    # VIX 보수모드 상향 임계 재적용(정상이면 3% 이미 통과).
    if (row.get("predicted_rise_pct") or 0) < effective_min_rise:
        return "below_caution_min"
    return None


# ──────────────────────────────────────────────────────────────────────────
#  VIX 시장 변동성 게이트 (반드시 T-1)
# ──────────────────────────────────────────────────────────────────────────
def vix_gate(rec_date: str) -> tuple[str, float | None, str | None]:
    """(regime, vix_t1, block_reason) 반환. regime ∈ {normal, caution, blocked}.

    VIX[T-1] = rec_date '이전' 최근 영업일 VIX(당일 제외 — look-ahead 차단).
    결측 또는 ffill 정체(직전 영업일들 동일값) → fail-close(blocked).
    """
    rows = market_data_repo.fetch_series_before(VIX_COL, rec_date, limit=15)
    wk = [
        (r[DATE_COL], r[VIX_COL]) for r in rows
        if r.get(VIX_COL) is not None and pd.Timestamp(r[DATE_COL]).weekday() < 5
    ]
    if not wk:
        logger.warning("VIX[T-1] 결측(%s 이전 영업일 없음) → 보수모드 차단(fail-close).", rec_date)
        return "blocked", None, "vix_missing"
    vix_t1 = float(wk[-1][1])
    # ffill 정체 의심: 직전 3영업일 VIX 가 모두 동일하면(>1일 ffill) 소스 갭 → fail-close.
    if len(wk) >= 3 and wk[-1][1] == wk[-2][1] == wk[-3][1]:
        logger.warning("VIX ffill 정체 의심(직전 3영업일 동일=%.2f) → 보수모드 차단.", vix_t1)
        return "blocked", vix_t1, "vix_stall"
    if vix_t1 >= settings.VIX_BLOCK:
        return "blocked", vix_t1, "vix_block"
    if vix_t1 >= settings.VIX_CAUTION:
        return "caution", vix_t1, None
    return "normal", vix_t1, None


# ──────────────────────────────────────────────────────────────────────────
#  정렬(결정적 tie-breaker)
# ──────────────────────────────────────────────────────────────────────────
def _sort_key(c: dict):
    parts = []
    for field, asc in SCORE_TIE_BREAKERS:
        v = c.get(field)
        if isinstance(v, str):
            parts.append(v)                    # 문자열은 asc 만 사용(ticker)
        else:
            num = float(v) if v is not None else float("-inf")
            parts.append(num if asc else -num)
    return tuple(parts)


# ──────────────────────────────────────────────────────────────────────────
#  오케스트레이션
# ──────────────────────────────────────────────────────────────────────────
def _ml_asof_date() -> str | None:
    """ML 분석 기준일(as-of) 근사 = stock_analysis_results 의 최신 created_at(KST 날짜).

    (정확한 '마지막 실제가 일자' 컬럼은 없어 산출 시각을 프록시로 사용. 기준일 격차 검사용.)
    """
    rows = analysis_repo.fetch_latest_analysis()
    stamps = [pd.Timestamp(r["created_at"]) for r in rows if r.get("created_at")]
    if not stamps:
        return None
    return str(max(stamps).tz_convert(KST).date())


def _score_candidate(row: dict) -> dict:
    """통과 후보에 서브스코어/종합점수/플래그를 붙인 dict 반환."""
    ml = ml_sub(row["predicted_rise_pct"])
    tech = tech_sub(
        row.get("golden_cross"), row.get("macd_buy"), row.get("rsi"),
        row.get("adx"), row.get("plus_di"), row.get("minus_di"),
    )
    flow = flow_sub(row["flow_score"], row.get("foreign_net"), row.get("institution_net"))
    vol = vol_sub(row.get("volume_ratio"))
    sig = flow_signals(row.get("foreign_net"), row.get("institution_net"))
    return {
        **row,
        "ml_sub": round(ml, 6), "tech_sub": round(tech, 6),
        "flow_sub": round(flow, 6), "vol_sub": round(vol, 6),
        "total_score": round(total_score(ml, tech, flow, vol), 6),
        "dual_buy": sig["dual_buy"],
    }


def score_and_decide(date: str | None = None) -> dict:
    """매수 후보 점수화 + VIX 게이트 + LLM 거부권 검토 → decision_logs 저장."""
    rec_date = date or datetime.now(KST).strftime("%Y-%m-%d")

    candidates = [r for r in recommendation_repo.fetch_by_date(rec_date) if r.get("is_candidate")]
    if not candidates:
        logger.warning("후보 0건(%s) — stock_recommendations 에 is_candidate 종목 없음.", rec_date)
        return {"date": rec_date, "candidates": 0, "buys": 0, "status": "no_candidates"}

    regime, vix_t1, block_reason = vix_gate(rec_date)
    effective_min_rise = (
        settings.VIX_CAUTION_MIN_RISE_PCT if regime == "caution"
        else settings.RECOMMEND_MIN_RISE_PCT
    )
    ml_asof = _ml_asof_date()
    logger.info(
        "점수 산정 시작 %s — VIX[T-1]=%s regime=%s, 실효최소상승=%.1f%%, ML기준일=%s, 후보=%d",
        rec_date, f"{vix_t1:.2f}" if vix_t1 is not None else "None",
        regime, effective_min_rise, ml_asof, len(candidates),
    )

    # ── 시장 차단(보수모드 blocked): 점수/LLM 생략, 전 후보 HOLD ──
    if regime == "blocked":
        logger.warning("시장 차단(%s) — 신규매수 전면 보류. 사유=%s", rec_date, block_reason)
        records = [_decision_record(c, rec_date, "HOLD", f"market_blocked:{block_reason}",
                                    "market_blocked", vix_t1, regime, None, None, False)
                   for c in candidates]
        decision_repo.replace_for_date(rec_date, records)
        return {"date": rec_date, "candidates": len(candidates), "buys": 0,
                "status": "market_blocked", "vix_t1": vix_t1, "regime": regime}

    # ── 하드필터 ──
    survivors: list[dict] = []
    filtered: list[dict] = []
    for c in candidates:
        reason = hard_filter(c, effective_min_rise, ml_asof)
        if reason is None:
            survivors.append(_score_candidate(c))
        else:
            filtered.append({**c, "_reason": reason})
            logger.info("하드필터 제외 %s(%s): %s", c.get("name"), c.get("ticker"), reason)

    # ── 정렬(결정적) + 순위 ──
    survivors.sort(key=_sort_key)
    for i, s in enumerate(survivors, start=1):
        s["rank"] = i

    # ── LLM 거부권 검토(통과 종목만). fail-close: 실패 시 전부 HOLD ──
    llm_ok, market_analysis, decisions, model = llm_review_service.review(
        survivors, vix_t1=vix_t1, regime=regime, market_date=rec_date
    )

    records: list[dict] = []
    buys = 0
    for s in survivors:
        dec = decisions.get(s["ticker"]) if (llm_ok and decisions) else None
        # 거부권만: LLM 이 명시적으로 BUY 한 통과 종목만 BUY. 그 외(미언급/HOLD/실패)=HOLD.
        decision = "BUY" if (dec and dec.get("decision") == "BUY") else "HOLD"
        reason = (dec.get("reason") if dec else None) or ("llm_failed_fail_close" if not llm_ok else "llm_hold")
        if decision == "BUY":
            buys += 1
        records.append(_decision_record(
            s, rec_date, decision, reason, "llm_reviewed", vix_t1, regime,
            market_analysis, model, llm_ok, scored=s,
        ))
    # 하드필터 탈락분도 감사 로그로 기록(HOLD).
    for f in filtered:
        records.append(_decision_record(
            f, rec_date, "HOLD", f["_reason"], "hard_filtered", vix_t1, regime,
            market_analysis, model, llm_ok,
        ))

    decision_repo.replace_for_date(rec_date, records)
    logger.info(
        "결정 저장 %s — 통과=%d, 매수=%d, 하드필터제외=%d, LLM=%s(%s)",
        rec_date, len(survivors), buys, len(filtered),
        "OK" if llm_ok else "FAIL-CLOSE", model or "-",
    )
    return {
        "date": rec_date, "candidates": len(candidates), "survivors": len(survivors),
        "buys": buys, "filtered": len(filtered), "regime": regime, "vix_t1": vix_t1,
        "llm_ok": llm_ok, "model": model, "status": "ok",
    }


def _decision_record(
    row: dict, rec_date: str, decision: str, reason: str, stage: str,
    vix_t1: float | None, regime: str, market_analysis: str | None,
    model: str | None, llm_ok: bool, scored: dict | None = None,
) -> dict:
    """decision_logs 행 구성. scored 가 있으면 서브스코어/순위 포함."""
    rec = {
        "decision_date": rec_date,
        "ticker": row.get("ticker"),
        "name": row.get("name"),
        "decision": decision,
        "reason": reason,
        "stage": stage,
        "predicted_rise_pct": row.get("predicted_rise_pct"),
        "flow_score": row.get("flow_score"),
        "vix_t1": vix_t1,
        "market_regime": regime,
        "market_analysis": market_analysis,
        "llm_model": model,
        "llm_ok": llm_ok,
        # 점수/순위(통과 종목만, 그 외 NULL — 멱등 키 통일)
        "total_score": None, "ml_sub": None, "tech_sub": None,
        "flow_sub": None, "vol_sub": None, "dual_buy": None, "rank": None,
    }
    if scored is not None:
        for k in ("total_score", "ml_sub", "tech_sub", "flow_sub", "vol_sub", "dual_buy", "rank"):
            rec[k] = scored.get(k)
    return rec


def list_for_date(date: str | None = None) -> dict:
    """특정 날짜(기본 최근)의 결정 로그 조회."""
    target = date or decision_repo.latest_date()
    if target is None:
        return {"date": None, "count": 0, "decisions": []}
    rows = decision_repo.fetch_by_date(target)
    return {"date": target, "count": len(rows), "decisions": rows}
