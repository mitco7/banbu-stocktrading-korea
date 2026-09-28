"""일일 파이프라인 오케스트레이션 — Step 1~5 를 순서대로 한 번에 실행.

실행 순서(각 단계는 앞 단계 결과에 의존하므로 순서 고정):
  1. 시장 데이터 증분 수집  (market_data_service.update_market_data — /market/update 와 동일)
  2. 수급(외국인·기관) 수집 (flow_service.collect_and_store_flows — /flow/collect 와 동일)
  3. ML(Kaggle) 트리거 + 완료 대기 (/ml/trigger + /ml/status 폴링, 최대 PIPELINE_ML_TIMEOUT_SEC)
  4. 매수 후보 생성          (recommendation_service.generate_recommendations)
  5. 종합점수 + VIX 게이트 + LLM 최종결정 (scoring_service.score_and_decide)

원칙:
- 어떤 단계든 실패하면 즉시 중단하고 이후 단계로 진행하지 않는다(fail-close, CLAUDE.md §2.4).
- 각 단계가 멱등이므로 파이프라인 전체도 같은 날짜로 재실행해도 안전하다(§2.7).
- ML 완료 판정의 1차 근거는 stock_analysis_results 의 신규 created_at(트리거 이후) —
  Kaggle status 엔드포인트가 서버측 장애(unavailable)를 자주 내기 때문(ml_trigger_service 참고).
"""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from core.config import settings
from db.repositories import analysis_repo, run_repo
from services import (
    flow_service,
    market_data_service,
    ml_trigger_service,
    notification_service,
    recommendation_service,
    scoring_service,
)
from utils.logger import get_logger

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")

# stock_recommendations 행에서 그룹핑 시 제외/분류할 키(§ _stock_detail).
_ML_KEYS = (
    "last_actual_price", "predicted_future_price", "predicted_rise_pct",
    "predicted_rise", "ml_recommendation",
)
_FLOW_KEYS = ("flow_score", "foreign_net", "institution_net", "flow_calc_date")
_META_KEYS = ("날짜", "id", "created_at", "ticker", "name")
_SCORE_KEYS = ("total_score", "ml_sub", "tech_sub", "flow_sub", "vol_sub", "dual_buy")


# ──────────────────────────────────────────────────────────────────────────
#  ML 완료 대기
# ──────────────────────────────────────────────────────────────────────────
def _ml_latest_created_at() -> pd.Timestamp | None:
    """stock_analysis_results 의 최신 created_at(비교용 기준선)."""
    rows = analysis_repo.fetch_latest_analysis()
    stamps = [pd.Timestamp(r["created_at"]) for r in rows if r.get("created_at")]
    return max(stamps) if stamps else None


def _wait_for_ml_completion(baseline: pd.Timestamp | None) -> dict:
    """Kaggle 실행 완료까지 폴링 대기.

    완료 판정(둘 중 하나):
      - stock_analysis_results 에 baseline 이후 created_at 이 생김(신규 결과 적재) — 1차 근거
      - (보조) kaggle status == complete 이면서 신규 결과도 확인됨
    실패 판정: status 가 error/cancel_* 이거나 타임아웃 초과.
    """
    interval = settings.PIPELINE_ML_POLL_INTERVAL_SEC
    timeout = settings.PIPELINE_ML_TIMEOUT_SEC
    started = time.monotonic()
    polls = 0
    last_status = "unknown"

    while True:
        elapsed = time.monotonic() - started
        if elapsed > timeout:
            return {"completed": False, "final_status": last_status, "polls": polls,
                    "elapsed_sec": round(elapsed, 1),
                    "error": f"ML 완료 대기 타임아웃({timeout:.0f}초) — 파이프라인 중단(fail-close)"}

        time.sleep(interval)
        polls += 1

        try:
            last_status = ml_trigger_service.get_status()
        except RuntimeError as exc:
            logger.warning("ML status 조회 실패(계속 폴링): %s", exc)
            last_status = "unknown"

        if last_status == "error" or last_status.startswith("cancel"):
            return {"completed": False, "final_status": last_status, "polls": polls,
                    "elapsed_sec": round(time.monotonic() - started, 1),
                    "error": f"Kaggle 커널 실패 상태: {last_status}"}

        # 1차 근거: 결과 테이블에 트리거 이후 시각의 행이 생겼는가.
        latest = _ml_latest_created_at()
        fresh = latest is not None and (baseline is None or latest > baseline)
        logger.info("ML 폴링 #%d — status=%s, 결과갱신=%s (%.0f초 경과)",
                    polls, last_status, fresh, time.monotonic() - started)
        if fresh:
            return {"completed": True, "final_status": last_status, "polls": polls,
                    "elapsed_sec": round(time.monotonic() - started, 1),
                    "results_created_at": str(latest)}


# ──────────────────────────────────────────────────────────────────────────
#  종목 상세 구성
# ──────────────────────────────────────────────────────────────────────────
def _stock_detail(decision_row: dict, rec_row: dict | None) -> dict:
    """decision_logs 행 + stock_recommendations 행을 종목 상세 1건으로 병합.

    1~5단계 산출물이 모두 보이도록 그룹핑: 결정(5) / 점수(5) / ML(3→4) / 지표(1→4) / 수급(2→4).
    """
    detail = {
        "ticker": decision_row.get("ticker"),
        "name": decision_row.get("name"),
        # ── 5단계: 최종 결정 ──
        "decision": decision_row.get("decision"),
        "reason": decision_row.get("reason"),
        "stage": decision_row.get("stage"),  # llm_reviewed / hard_filtered / market_blocked
        "rank": decision_row.get("rank"),
        "llm_model": decision_row.get("llm_model"),  # 알림에 AI 검토 주체 표기
        # ── 5단계: 종합점수(하드필터 탈락·차단 종목은 NULL) ──
        "scores": {k: decision_row.get(k) for k in _SCORE_KEYS},
    }
    if rec_row:
        detail["ml_prediction"] = {k: rec_row.get(k) for k in _ML_KEYS}
        detail["ml_prediction"]["is_candidate"] = rec_row.get("is_candidate")
        detail["ml_prediction"]["rise_threshold"] = rec_row.get("rise_threshold")
        detail["investor_flow"] = {k: rec_row.get(k) for k in _FLOW_KEYS}
        skip = set(_ML_KEYS) | set(_FLOW_KEYS) | set(_META_KEYS) | {"is_candidate", "rise_threshold"}
        detail["technical_indicators"] = {
            k: v for k, v in rec_row.items() if k not in skip
        }
    else:
        detail["ml_prediction"] = None
        detail["investor_flow"] = None
        detail["technical_indicators"] = None
    return detail


def _build_stock_details(rec_date: str) -> dict:
    """rec_date 의 결정 로그 + 추천 행을 병합해 BUY/HOLD/후보미달 상세 목록 구성."""
    decisions = scoring_service.list_for_date(rec_date).get("decisions") or []
    recs = recommendation_service.list_for_date(rec_date).get("recommendations") or []
    rec_by_ticker = {r["ticker"]: r for r in recs}

    buy_stocks: list[dict] = []
    hold_stocks: list[dict] = []
    decided_tickers: set[str] = set()
    llm_market_analysis = None
    for d in decisions:
        decided_tickers.add(d.get("ticker"))
        if llm_market_analysis is None and d.get("market_analysis"):
            llm_market_analysis = d["market_analysis"]
        detail = _stock_detail(d, rec_by_ticker.get(d.get("ticker")))
        (buy_stocks if d.get("decision") == "BUY" else hold_stocks).append(detail)

    # 예측 상승률이 임계값 미달이라 후보조차 안 된 종목(결정 로그 없음)도 상세 제공.
    non_candidates = [
        _stock_detail({"ticker": r["ticker"], "name": r.get("name"),
                       "decision": "NOT_CANDIDATE",
                       "reason": f"predicted_rise_pct < {r.get('rise_threshold')}%",
                       "stage": "pre_filter"}, r)
        for r in recs
        if r["ticker"] not in decided_tickers
    ]

    return {
        "llm_market_analysis": llm_market_analysis,
        "buy": {"count": len(buy_stocks), "stocks": buy_stocks},
        "hold": {"count": len(hold_stocks), "stocks": hold_stocks},
        "non_candidates": {"count": len(non_candidates), "stocks": non_candidates},
    }


# ──────────────────────────────────────────────────────────────────────────
#  Slack 알림 메시지 구성
# ──────────────────────────────────────────────────────────────────────────
def _collect_summary(market_result: dict, flow_result: dict) -> str:
    """1~2단계(시장 데이터 + 수급) 수집 결과를 Slack 1건용 요약으로 압축."""
    status = market_result.get("status")
    if status == "collected":
        market_line = (f"시장 데이터: {market_result.get('start')} ~ {market_result.get('end')} "
                       f"수집 — {market_result.get('upserted', 0)}행 upsert")
    elif status == "up_to_date":
        market_line = "시장 데이터: 이미 최신 (신규 수집 없음)"
    else:  # range_too_large 등 비정상 상태는 그대로 노출해 조기 인지
        market_line = f"시장 데이터: {status} (upsert {market_result.get('upserted', 0)}행)"
    flow_line = (f"수급: {flow_result.get('stored')}종목 저장 "
                 f"(기준일 {flow_result.get('calculation_date')}, "
                 f"lookback {flow_result.get('lookback_days')}일)")
    return f"{market_line}\n{flow_line}"


# VIX 게이트 국면 → 사람이 읽는 설명(알림용).
_REGIME_LABEL = {
    "normal": "평시 — 신규 매수 허용",
    "caution": "주의 — 매수 기준 강화(최소 예측상승률 상향)",
    "blocked": "차단 — 신규 매수 중단(보수 모드)",
}


def _clip(text, limit: int) -> str:
    """알림용 한 줄 축약(개행·연속 공백 제거 후 limit 자 초과분 말줄임)."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _eok(v) -> str:
    """원 → 억/조 표기(부호 포함). 수급 순매수액 알림용."""
    if not isinstance(v, (int, float)):
        return "-"
    if abs(v) >= 1e12:
        return f"{v / 1e12:+,.1f}조"
    return f"{v / 1e8:+,.0f}억"


_RSI_LABEL = {"overbought": "과열", "oversold": "과매도", "neutral": "중립"}


def _won(v, nd: int = 0) -> str:
    return f"{v:,.{nd}f}원" if isinstance(v, (int, float)) else "-"


def _auto(v) -> str:
    """지표 값 표기 — 큰 값은 정수, 작은 값은 소수 2자리(종목별 가격대 차이 흡수)."""
    if not isinstance(v, (int, float)):
        return "-"
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}"


def _yn(flag) -> str:
    return "O" if flag else "X"


def _plan_lines(ml: dict, ind: dict) -> list[str]:
    """매매 계획 — 기준가와 ATR 몇 배/몇 %로 익절·손절을 잡는지 숫자로."""
    base, atr, atr_pct = ml.get("last_actual_price"), ind.get("atr"), ind.get("atr_pct")
    if not (isinstance(base, (int, float)) and base > 0 and isinstance(atr, (int, float)) and atr > 0):
        return []
    tp, sl = base + atr * settings.TP_ATR_MULT, base - atr * settings.SL_ATR_MULT
    pct_txt = f"주가의 {atr_pct:.2f}%" if isinstance(atr_pct, (int, float)) else "비율 미상"
    asof = ind.get("indicator_date")
    return [
        f"매수 기준가 {_won(base)}({asof + ' ' if asof else ''}종가) → "
        f"익절 {_won(tp)}({(tp / base - 1) * 100:+.1f}%) / 손절 {_won(sl)}({(sl / base - 1) * 100:+.1f}%)",
        f"   ATR {_won(atr)}({pct_txt}) 기준 — 익절 ATR×{settings.TP_ATR_MULT:g} / 손절 "
        f"ATR×{settings.SL_ATR_MULT:g}, {settings.TIME_STOP_DAYS}영업일 내 미도달 시 시간 청산",
    ]


def _tech_detail(ind: dict) -> str:
    """기술지표 세부항목 — 종합점수의 '기술' 항이 왜 그 값인지 보이도록 항목별 수치."""
    bits: list[str] = []
    sma_s, sma_l = ind.get("sma_short"), ind.get("sma_long")
    if isinstance(sma_s, (int, float)) and isinstance(sma_l, (int, float)):
        bits.append(f"골든크로스 {_yn(ind.get('golden_cross'))}"
                    f"(SMA{settings.SMA_SHORT} {sma_s:,.0f} vs SMA{settings.SMA_LONG} {sma_l:,.0f})")
    macd, sig = ind.get("macd"), ind.get("macd_signal")
    if macd is not None and sig is not None:
        bits.append(f"MACD 매수 {_yn(ind.get('macd_buy'))}({_auto(macd)} vs 시그널 {_auto(sig)})")
    rsi = ind.get("rsi")
    if isinstance(rsi, (int, float)):
        label = _RSI_LABEL.get(ind.get("rsi_signal") or "", "")
        bits.append(f"RSI {rsi:.1f}{f'({label})' if label else ''}")
    adx, p_di, m_di = ind.get("adx"), ind.get("plus_di"), ind.get("minus_di")
    if isinstance(adx, (int, float)):
        di = (f"(+DI {p_di:.1f} / -DI {m_di:.1f})"
              if isinstance(p_di, (int, float)) and isinstance(m_di, (int, float)) else "")
        bits.append(f"ADX {adx:.1f} 추세 {_yn(ind.get('trend_strong'))}{di}")
    vol = ind.get("volume_ratio")
    if isinstance(vol, (int, float)):
        bits.append(f"거래량 평소의 {vol:.2f}배")
    return " · ".join(bits)


def _score_math(sc: dict) -> str:
    """종합점수 산식 — 부문 점수 × 가중치의 합으로 총점이 나오는 과정을 그대로 보여준다."""
    terms = [(sc.get(k), label, w) for k, label, w in (
        ("ml_sub", "ML", settings.W_ML), ("tech_sub", "기술", settings.W_TECH),
        ("flow_sub", "수급", settings.W_FLOW), ("vol_sub", "변동성", settings.W_VOL))]
    parts = [f"{label} {v:.2f}×{w * 100:.0f}%" for v, label, w in terms if isinstance(v, (int, float))]
    if not parts:
        return ""
    total = sc.get("total_score")
    tail = f" = {total:.3f}" if isinstance(total, (int, float)) else ""
    return "종합점수 산식: " + " + ".join(parts) + tail


def _stock_block(idx: int, s: dict) -> str:
    """BUY 종목 1건 — 매매 계획과 판단 근거(ML·수급·기술지표·점수 산식·AI 검토) 전부."""
    ml = s.get("ml_prediction") or {}
    fl = s.get("investor_flow") or {}
    ind = s.get("technical_indicators") or {}
    sc = s.get("scores") or {}
    total = sc.get("total_score")
    head = f"*{idx}. {s.get('name')}({s.get('ticker')})*"
    if isinstance(total, (int, float)):
        head += f" — 종합점수 {total:.3f}"
    lines = [head, *_plan_lines(ml, ind)]

    # ① ML: 14일 후 예측가와 상승률(확률 아님 — CLAUDE.md §2.2), 후보 진입 기준과 함께.
    rise, future = ml.get("predicted_rise_pct"), ml.get("predicted_future_price")
    if isinstance(rise, (int, float)):
        threshold = ml.get("rise_threshold")
        base_txt = f" {_won(future)}" if isinstance(future, (int, float)) else ""
        th_txt = (f", 후보 기준 {threshold:.1f}% 이상" if isinstance(threshold, (int, float)) else "")
        sub = sc.get("ml_sub")
        sub_txt = f" → ML점수 {sub:.2f}" if isinstance(sub, (int, float)) else ""
        lines.append(f"① ML 예측: {settings.ML_FORECAST_DAYS}거래일 후{base_txt} ({rise:+.2f}%{th_txt}){sub_txt}")

    # ② 수급: 외국인·기관 순매수 금액과 쌍끌이 여부(한국 시장 특화 신호).
    f_net, i_net, flow = fl.get("foreign_net"), fl.get("institution_net"), fl.get("flow_score")
    if f_net is not None or i_net is not None or flow is not None:
        dual = " · 쌍끌이(외국인+기관 동반 순매수)" if sc.get("dual_buy") else ""
        calc = fl.get("flow_calc_date")
        flow_txt = f", flow_score {flow:+.2f}" if isinstance(flow, (int, float)) else ""
        sub = sc.get("flow_sub")
        sub_txt = f" → 수급점수 {sub:.2f}" if isinstance(sub, (int, float)) else ""
        lines.append(
            f"② 수급({calc or '최근'} 기준 {settings.FLOW_LOOKBACK_DAYS}거래일 합): "
            f"외국인 {_eok(f_net)} · 기관 {_eok(i_net)}{dual}{flow_txt}{sub_txt}"
        )

    # ③ 기술지표 세부항목.
    tech = _tech_detail(ind)
    if tech:
        sub = sc.get("tech_sub")
        sub_txt = f" → 기술점수 {sub:.2f}" if isinstance(sub, (int, float)) else ""
        lines.append(f"③ 기술지표: {tech}{sub_txt}")

    # ④ 변동성(ATR%) — 낮을수록 점수가 높다(같은 ATR 배수로 손절폭이 좁아짐).
    atr_pct, vol_sub = ind.get("atr_pct"), sc.get("vol_sub")
    if isinstance(atr_pct, (int, float)) or isinstance(vol_sub, (int, float)):
        pct_txt = f"ATR {atr_pct:.2f}%" if isinstance(atr_pct, (int, float)) else "ATR 비율 미상"
        sub_txt = f" → 변동성점수 {vol_sub:.2f}" if isinstance(vol_sub, (int, float)) else ""
        lines.append(f"④ 변동성: {pct_txt}{sub_txt}")

    math = _score_math(sc)
    if math:
        lines.append(math)
    if s.get("reason"):
        model = s.get("llm_model")
        lines.append(f"AI 최종 검토{f'({model})' if model else ''}: {_clip(s['reason'], 320)}")
    return "\n".join(lines)


def _buy_summary(details: dict, decision_result: dict, market_blocked: bool) -> str:
    """5단계 최종 매수 결정(BUY 목록)을 근거·예상 매매가까지 담아 Slack 1건으로 요약."""
    lines: list[str] = []

    # 시장 컨텍스트 — VIX 게이트 판정과 AI 시장 분석을 사람이 읽는 문장으로.
    vix, regime = decision_result.get("vix_t1"), decision_result.get("regime")
    if vix is not None or regime is not None:  # no_candidates 경로는 게이트 미평가 → 줄 생략
        vix_txt = f"{vix:.2f}" if isinstance(vix, (int, float)) else "-"
        lines.append(f"시장 상태: VIX(전일 종가) {vix_txt} · {_REGIME_LABEL.get(regime, regime or '-')}")
    if details.get("llm_market_analysis"):
        lines.append(f"AI 시장 분석: {_clip(details['llm_market_analysis'], 300)}")

    if details.get("buy") is None:
        # 상세 병합 실패 폴백 — '없음'으로 단언하면 제목의 N종목과 모순되므로 구분 표기.
        lines.append(f"종목 상세 조회 실패 — 매수 {decision_result.get('buys', 0)}종목은 "
                     "decision_logs 에서 확인 필요")
        return "\n".join(lines)

    stocks = details["buy"].get("stocks") or []
    if not stocks:
        lines.append("매수 종목 없음"
                     + (" — VIX 임계 초과로 신규 매수 차단(보수 모드)" if market_blocked
                        else " — 기준 통과 종목 없음(점수 미달·AI 보류)"))
        # 0건인 날은 '왜 없는지'가 정보다 — AI 검토까지 갔다가 보류된 상위 후보의 사유를 보여준다.
        reviewed = [h for h in ((details.get("hold") or {}).get("stocks") or [])
                    if h.get("stage") == "llm_reviewed" and h.get("reason")]
        reviewed.sort(key=lambda h: (h.get("scores") or {}).get("total_score") or 0, reverse=True)
        if reviewed:
            lines.append("")
            lines.append("보류된 상위 후보")
            for h in reviewed[:3]:
                score = (h.get("scores") or {}).get("total_score")
                score_txt = f" 종합점수 {score:.3f}" if isinstance(score, (int, float)) else ""
                lines.append(f"• {h.get('name')}({h.get('ticker')}){score_txt} — {_clip(h['reason'], 160)}")
    for i, s in enumerate(stocks, 1):
        lines.append("")
        lines.append(_stock_block(i, s))

    hold_cnt = (details.get("hold") or {}).get("count") or 0
    nc_cnt = (details.get("non_candidates") or {}).get("count") or 0
    rest: list[str] = []
    if hold_cnt:
        rest.append(f"HOLD {hold_cnt}종목(점수 미달·AI 보류 등)")
    if nc_cnt:
        rest.append(f"후보 제외 {nc_cnt}종목(예측상승률 미달)")
    if rest:
        lines.append("")
        lines.append("그 외: " + " · ".join(rest))
    if stocks and not settings.TRADING_ENABLED:
        # 주문 집행이 꺼진 기간엔 '오늘 집행' 문구가 거짓이 된다 — 추천임을 명시.
        lines.append(
            f"※ 지금은 주문 집행이 꺼져 있어(TRADING_ENABLED=false) 실제 매수·매도는 하지 않습니다. "
            f"목표·손절가는 전일 종가와 ATR(익절 ×{settings.TP_ATR_MULT:g} / 손절 "
            f"×{settings.SL_ATR_MULT:g}) 기준 참고용 예상치입니다."
        )
    elif stocks:
        lines.append(
            f"※ 매수는 오늘 {settings.SCHEDULE_BUY_TIME} 시장가 집행. 목표·손절가는 전일 종가와 "
            f"ATR(익절 ×{settings.TP_ATR_MULT:g} / 손절 ×{settings.SL_ATR_MULT:g}) 기준 예상치 — "
            f"확정가는 체결가 기준으로 '매수 체결 확인' 알림에 담깁니다. "
            f"{settings.TIME_STOP_DAYS}영업일 내 미도달 시 시간 청산."
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
#  파이프라인 본체
# ──────────────────────────────────────────────────────────────────────────
def run_daily_pipeline(force_market: bool = False, skip_ml: bool = False) -> dict:
    """Step 1~5 순차 실행. 단계 실패 시 그 자리에서 중단하고 지금까지의 결과를 반환.

    force_market: 장중에도 당일 포함 수집(/market/update?force=true 와 동일).
    skip_ml: Kaggle 트리거·대기 생략(이미 신선한 ML 결과가 있을 때). 결과가 stale 이면
             4단계의 신선도 검사(fail-close)가 알아서 중단시키므로 안전하다.
    """
    rec_date = datetime.now(KST).strftime("%Y-%m-%d")
    steps: list[dict] = []
    run_id = run_repo.start("daily_pipeline")

    def _fail(step_no: int, name: str, error: str, result: dict | None = None) -> dict:
        steps.append({"step": step_no, "name": name, "status": "failed",
                      "error": error, "result": result})
        logger.error("파이프라인 %d단계(%s) 실패 — 중단: %s", step_no, name, error)
        run_repo.finish(run_id, "failed", error=f"{step_no}단계({name}): {error}",
                        result={"steps": steps})
        notification_service.notify_error(
            f"파이프라인 {step_no}단계({name}) 실패 — 중단",
            f"{error}\n오늘({rec_date}) 매수 결정 없음(fail-close). 원인 해결 후 POST /pipeline/run 재실행 필요.",
        )
        return {"date": rec_date, "status": "failed", "failed_step": step_no,
                "steps": steps, "market_blocked": None,
                "buy": None, "hold": None, "non_candidates": None}

    # ── 1단계: 시장 데이터 증분 수집 ──────────────────────
    logger.info("파이프라인 시작(%s) — 1단계: 시장 데이터 수집", rec_date)
    try:
        market_result = market_data_service.update_market_data(force=force_market)
    except Exception as exc:  # noqa: BLE001 — 단계 실패를 응답으로 전달(fail-close)
        return _fail(1, "market_collect", str(exc))
    steps.append({"step": 1, "name": "market_collect", "status": "ok", "result": market_result})

    # ── 2단계: 수급 수집 ─────────────────────────────────
    logger.info("2단계: 수급(외국인·기관) 수집")
    try:
        flow_result = flow_service.collect_and_store_flows()
    except Exception as exc:  # noqa: BLE001
        return _fail(2, "flow_collect", str(exc))
    if not flow_result.get("stored"):
        # 수급 0건이면 flow_score 전부 NULL → 점수 왜곡. 여기서 중단(fail-close).
        return _fail(2, "flow_collect", "수급 데이터 0건 — 소스 접속/파싱 실패", flow_result)
    steps.append({"step": 2, "name": "flow_collect", "status": "ok", "result": flow_result})
    # 알림 케이스 1) 데이터 수집 완료 — 시장 데이터 + 수급 모두 성공한 시점에 1건 발송.
    # 시장 데이터가 비정상 상태(range_too_large 등 수집 거부)면 제목 '완료'가 오해를 낳고
    # NOTIFY_LEVEL=warn 환경에서 침묵하므로 warn 으로 구분 발송.
    collect_msg = _collect_summary(market_result, flow_result)
    if market_result.get("status") in ("collected", "up_to_date"):
        notification_service.notify_info(f"데이터 수집 완료 ({rec_date})", collect_msg)
    else:
        notification_service.notify_warn(
            f"데이터 수집 이상 ({rec_date}) — 시장 데이터 미갱신",
            collect_msg + "\n파이프라인은 계속 진행되지만 market_data 가 낡았을 수 있음 — 확인 필요.",
        )

    # ── 3단계: ML 트리거 + 완료 대기 ─────────────────────
    if skip_ml:
        logger.info("3단계: ML 트리거 생략(skip_ml=true) — 기존 결과 사용(신선도는 4단계가 검증)")
        steps.append({"step": 3, "name": "ml_predict", "status": "skipped",
                      "result": {"skipped": True}})
    else:
        logger.info("3단계: ML(Kaggle) 트리거 + 완료 대기(최대 %.0f초)",
                    settings.PIPELINE_ML_TIMEOUT_SEC)
        try:
            baseline = _ml_latest_created_at()
            pushed, push_msg = ml_trigger_service.push_kernel()
        except Exception as exc:  # noqa: BLE001
            return _fail(3, "ml_predict", str(exc))
        if not pushed:
            return _fail(3, "ml_predict", f"Kaggle push 실패: {push_msg}")
        try:
            wait_result = _wait_for_ml_completion(baseline)
        except Exception as exc:  # noqa: BLE001
            return _fail(3, "ml_predict", str(exc), {"push": push_msg})
        if not wait_result.get("completed"):
            return _fail(3, "ml_predict", wait_result.get("error", "ML 완료 실패"),
                         {"push": push_msg, **wait_result})
        steps.append({"step": 3, "name": "ml_predict", "status": "ok",
                      "result": {"push": push_msg, **wait_result}})

    # ── 4단계: 매수 후보 생성 ────────────────────────────
    logger.info("4단계: 매수 후보 생성")
    try:
        rec_result = recommendation_service.generate_recommendations(rec_date)
    except Exception as exc:  # noqa: BLE001
        return _fail(4, "recommendations_generate", str(exc))
    if rec_result.get("status") in ("no_ml_data", "stale_ml"):
        return _fail(4, "recommendations_generate",
                     f"ML 결과 이상({rec_result['status']}) — 후보 생성 불가", rec_result)
    steps.append({"step": 4, "name": "recommendations_generate", "status": "ok",
                  "result": rec_result})

    # ── 5단계: 종합점수 + VIX 게이트 + LLM 최종결정 ──────
    logger.info("5단계: 종합점수 + VIX 게이트 + LLM 최종결정")
    try:
        decision_result = scoring_service.score_and_decide(rec_date)
    except Exception as exc:  # noqa: BLE001
        return _fail(5, "decisions_run", str(exc))
    steps.append({"step": 5, "name": "decisions_run", "status": "ok",
                  "result": decision_result})

    # ── 종목별 상세(BUY/HOLD/후보미달) 병합 ──────────────
    try:
        details = _build_stock_details(rec_date)
    except Exception as exc:  # noqa: BLE001 — 상세 조회 실패해도 단계 결과는 반환
        logger.warning("종목 상세 병합 실패(단계 결과만 반환): %s", exc)
        details = {"llm_market_analysis": None, "buy": None, "hold": None,
                   "non_candidates": None}

    market_blocked = decision_result.get("status") == "market_blocked"
    logger.info("파이프라인 완료(%s) — 매수=%d, market_blocked=%s",
                rec_date, decision_result.get("buys", 0), market_blocked)
    run_repo.finish(run_id, "ok", result={"steps": steps,
                                          "buys": decision_result.get("buys", 0),
                                          "market_blocked": market_blocked})
    # 알림 케이스 2) 오늘의 매수 종목 추출 — 파이프라인 완주 시점에 BUY 목록 상세 발송.
    notification_service.notify_info(
        f"오늘의 매수 종목 {decision_result.get('buys', 0)}종목 ({rec_date})",
        _buy_summary(details, decision_result, market_blocked),
    )
    return {
        "date": rec_date,
        "status": "ok",
        "failed_step": None,
        "market_blocked": market_blocked,
        "vix_t1": decision_result.get("vix_t1"),
        "regime": decision_result.get("regime"),
        "llm_market_analysis": details.get("llm_market_analysis"),
        "steps": steps,
        "buy": details.get("buy"),
        "hold": details.get("hold"),
        "non_candidates": details.get("non_candidates"),
    }
