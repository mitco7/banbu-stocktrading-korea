"""KIS 매수/매도 + ATR 익절·손절 + 정합성 동기화 (Step 6).

흐름:
  execute_buys(date)   : decision_logs 의 BUY 종목을 중복방지·10% 배분·지정가 매수, ATR TP/SL 저장.
  run_sell_cycle()     : 보유 종목을 1분 주기로 점검 — TP/SL(시장가, 손절 우선)·기술신호·VIX·time-stop.
  reconcile()          : KIS 잔고 ↔ trade_records 상태 동기화(체결 전이·고아 INSERT).

안전 원칙(CLAUDE.md):
  - 주문은 **재시도 금지**(§2.7). 주문 송신 전 INSERT(buy_ordered)로 의도 기록.
  - 손절은 **시장가 청산**(지정가 금지 — 갭다운 미체결로 손실 확대). 동시 트리거 시 **손절 우선**.
  - ATR 백필은 **buy_date 기준**(호출 시점 ATR 금지, §2.4). 백필 실패 시 risk_unmanaged 플래그.
  - 배수·임계는 전부 core/config.py 외부화.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from core import market_calendar
from core.config import settings
from db.repositories import decision_repo, flow_repo, trade_repo
from services import balance_service, indicators as ind, kis_client, notification_service, scoring_service
from services.collectors.stock_collector import fetch_stock_ohlcv
from utils.hoga import clamp_to_limit
from utils.logger import get_logger

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")
_ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
_HIST_DAYS = 200  # 지표/ATR 계산용 일봉 조회 기간(달력일)


def _now_kst() -> datetime:
    """현재 KST 시각 — 테스트에서 monkeypatch 로 고정할 수 있게 분리."""
    return datetime.now(KST)


def _parse_hhmm(value: str) -> dtime:
    h, m = value.split(":")
    return dtime(int(h), int(m))


def _fetch_ohlcv_safe(code: str, start: str, end: str) -> pd.DataFrame | None:
    """FDR 일봉 조회 wall-clock 타임아웃 래핑(FDR 는 timeout 미노출 → 무한대기 방지).

    매도 감시 루프가 원격 조회 hang 으로 멈추면 손절 판정 자체가 정지하므로 필수.
    초과/예외 시 None(해당 종목만 제외).
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


def _tr(real_id: str, mock_id: str) -> str:
    return mock_id if settings.KIS_USE_MOCK else real_id


# ──────────────────────────────────────────────────────────────────────────
#  순수 함수 (테스트 용이)
# ──────────────────────────────────────────────────────────────────────────
def compute_tp_sl(buy_price: float, atr: float | None) -> tuple[float | None, float | None]:
    """ATR 기반 익절가/손절가(KRW). ATR 없으면 (None, None) → 백필 대상."""
    if atr is None or not np.isfinite(atr) or atr <= 0:
        return None, None
    tp = buy_price + atr * settings.TP_ATR_MULT
    sl = buy_price - atr * settings.SL_ATR_MULT
    return round(tp, 2), round(sl, 2)


def _busdays(start: str, end: str) -> int:
    """KRX 영업일 수([start, end) — 주말 + 한국 휴장일 제외)."""
    return market_calendar.krx_busdays(start, end)


def evaluate_sell_signals(daily: pd.DataFrame, quote: dict, flow_score: float | None) -> dict:
    """기술적 매도신호 4종을 daily 일봉(종가 확정분) + 실시간 quote 로 판정.

    ① 데드크로스(SMA20<SMA60) ② RSI>SELL_RSI_OVERBOUGHT ③ MACD<Signal ④ 패닉셀(거래량 급증+급락)
    Returns: {"count", "dead_cross", "rsi_over", "macd_below", "panic"}.
    """
    close = daily["close"]
    sma_s = ind.sma(close, settings.SMA_SHORT).iloc[-1]
    sma_l = ind.sma(close, settings.SMA_LONG).iloc[-1]
    rsi = ind.rsi_wilder(close, settings.RSI_PERIOD).iloc[-1]
    macd_line, signal_line, _ = ind.macd(close, settings.MACD_FAST, settings.MACD_SLOW, settings.MACD_SIGNAL)

    dead_cross = bool(np.isfinite(sma_s) and np.isfinite(sma_l) and sma_s < sma_l)
    rsi_over = bool(np.isfinite(rsi) and rsi > settings.SELL_RSI_OVERBOUGHT)
    macd_below = bool(
        np.isfinite(macd_line.iloc[-1]) and np.isfinite(signal_line.iloc[-1])
        and macd_line.iloc[-1] < signal_line.iloc[-1]
    )
    # 패닉셀: 당일 급락(전일대비율 <= PANIC_DROP_PCT).
    # ⚠ 거래량 조건(PANIC_VOL_MULT)은 제거 — 장중 누적거래량(quote) vs 종일평균 비교는 시간편향·
    #    look-ahead 왜곡(오전엔 못 잡고 오후엔 과트리거)이라 stock-quant 권고로 급락 단독 정의.
    #    (PANIC_VOL_MULT 는 체결조회 기반 정밀 구현 시 부활 — 현재 config 에 보존만.)
    change_pct = quote.get("change_pct")
    panic = bool(change_pct is not None and change_pct <= settings.PANIC_DROP_PCT)

    count = sum([dead_cross, rsi_over, macd_below, panic])
    return {"count": count, "dead_cross": dead_cross, "rsi_over": rsi_over,
            "macd_below": macd_below, "panic": panic}


def decide_sell(
    *, sl_hit: bool, tp_hit: bool, time_stop: bool,
    signal_count: int, flow_score: float | None, vix_t1: float | None,
) -> tuple[bool, str | None]:
    """매도 여부·사유 결정. 우선순위: 손절 > 익절 > 시간청산 > 기술신호(시장변동성 가중)."""
    if sl_hit:
        return True, "stop_loss"        # 손절 우선(동시 트리거 시)
    if tp_hit:
        return True, "take_profit"
    if time_stop:
        return True, "time_stop"
    # 기술신호 + 시장국면 가중(VIX[T-1]).
    n = signal_count
    flow_bad = flow_score is not None and flow_score < settings.SELL_FLOW_BAD
    vix = vix_t1
    if n >= settings.SELL_SIGNAL_THRESHOLD:
        return True, "signal"
    if n >= 2 and flow_bad:
        return True, "signal_flow"
    if vix is not None and vix >= settings.VIX_BLOCK and n >= settings.SELL_VIX_BLOCK_SIGNALS:
        return True, "signal_vix_block"
    if vix is not None and vix >= settings.VIX_CAUTION and n >= settings.SELL_VIX_CAUTION_SIGNALS:
        return True, "signal_vix_caution"
    return False, None


# ──────────────────────────────────────────────────────────────────────────
#  ATR (as-of 일자 기준)
# ──────────────────────────────────────────────────────────────────────────
def _compute_atr_asof(code: str, asof_date: str) -> float | None:
    """asof_date 까지의 일봉으로 ATR(Wilder) 계산. 매수일자 기준 백필에 사용."""
    start = (pd.Timestamp(asof_date) - pd.Timedelta(days=_HIST_DAYS)).strftime("%Y-%m-%d")
    df = _fetch_ohlcv_safe(code, start, asof_date)
    if df is None or len(df) < settings.ATR_PERIOD + 1:
        return None
    atr = ind.atr_wilder(df["high"], df["low"], df["close"], settings.ATR_PERIOD).iloc[-1]
    return float(atr) if np.isfinite(atr) else None


# ──────────────────────────────────────────────────────────────────────────
#  매수
# ──────────────────────────────────────────────────────────────────────────
def execute_buys(date: str | None = None) -> dict:
    """decision_logs 의 BUY 종목 매수. 중복방지·10% 배분·지정가·ATR TP/SL 저장.

    무인 운영 가드(§2.4):
    - 휴장일/주문시간창 밖이면 주문 없이 스킵(스케줄러가 휴일에 돌아도 안전).
    - 결정일(rec_date)이 오늘이 아니면 주문 금지(stale_decisions) — 파이프라인이 실패한 날
      어제 결정을 오늘 가격으로 집행하는 사고 차단(fail-close).
    """
    env = settings.kis_environment
    now = _now_kst()
    today = now.strftime("%Y-%m-%d")

    # ── 가드 1: 거래일 + 매수 주문 시간창 ──
    if not market_calendar.is_trading_day(now):
        logger.info("매수 스킵 — 오늘(%s)은 KRX 휴장일/주말", today)
        return {"date": today, "ordered": 0, "status": "market_closed"}
    win_start, win_end = _parse_hhmm(settings.BUY_ORDER_START), _parse_hhmm(settings.BUY_ORDER_END)
    now_t = now.timetz().replace(tzinfo=None)
    if not (win_start <= now_t <= win_end):
        logger.info("매수 스킵 — 주문 시간창(%s~%s) 밖 (현재 %s)",
                    settings.BUY_ORDER_START, settings.BUY_ORDER_END, now_t.strftime("%H:%M"))
        # 무인 운영에서 침묵하면 스케줄 시각 오설정을 영원히 모름 — warn 으로 표면화.
        notification_service.notify_warn(
            "매수 스킵 — 주문 시간창 밖",
            f"현재 {now_t.strftime('%H:%M')}, 허용 {settings.BUY_ORDER_START}~{settings.BUY_ORDER_END}. "
            "스케줄 실행 시각 확인 필요.",
        )
        return {"date": today, "ordered": 0, "status": "outside_order_window"}

    rec_date = date or decision_repo.latest_date()
    if rec_date is None:
        return {"date": None, "ordered": 0, "status": "no_decisions"}

    # ── 가드 2: 결정 신선도 — 오늘 결정만 집행(fail-close) ──
    if rec_date != today:
        logger.warning("매수 차단 — 결정일(%s)이 오늘(%s)이 아님. 파이프라인 미실행/실패 여부 확인 필요.",
                       rec_date, today)
        notification_service.notify_warn(
            "매수 차단(stale_decisions)",
            f"최근 결정일 {rec_date} ≠ 오늘 {today}. 오늘 파이프라인이 실패했거나 아직 돌지 않음 — 주문 없음(fail-close).",
        )
        return {"date": rec_date, "ordered": 0, "status": "stale_decisions"}

    buys = [r for r in decision_repo.fetch_by_date(rec_date) if r.get("decision") == "BUY"]
    if not buys:
        return {"date": rec_date, "ordered": 0, "status": "no_buy_decisions"}

    try:
        bal = balance_service.fetch_balance()
    except Exception as exc:  # noqa: BLE001 — 주문 전 단계 실패도 무인 운영에선 알림 필수
        notification_service.notify_error(
            "매수 실행 실패(잔고 조회)", f"주문 0건 송신 — {type(exc).__name__}: {exc}"
        )
        raise
    dedup = set(bal["holdings"]) | trade_repo.open_tickers(env)  # KIS 보유 ∪ DB 미청산
    base_cash = bal["cash"]
    remaining = base_cash

    ordered, skipped = [], []
    for d in buys:
        ticker, name = d["ticker"], d.get("name")
        if ticker in dedup:
            skipped.append((name, "already_open"))
            continue
        # 시세/주문가능 조회 실패는 해당 종목만 스킵 — 예외 전파 시 이미 접수된 앞 종목의
        # 주문 알림까지 통째로 유실되고 나머지 BUY 종목도 미집행되기 때문.
        try:
            quote = balance_service.fetch_price(ticker)
        except Exception as exc:  # noqa: BLE001
            logger.error("시세 조회 실패 %s(%s): %s — 스킵", name, ticker, exc)
            skipped.append((name, "quote_failed"))
            notification_service.notify_error("매수 스킵(시세 조회 실패)", f"{name}({ticker}): {exc}")
            continue
        price = quote["price"]
        if price <= 0:
            skipped.append((name, "no_price"))
            continue
        limit_price = clamp_to_limit(price, quote["prev_close"] or price)
        # 배분: 기준 현금의 10%, 남은 현금·매수가능수량으로 캡.
        alloc = base_cash * settings.POSITION_PCT
        try:
            orderable = balance_service.fetch_orderable(ticker, limit_price)
        except Exception as exc:  # noqa: BLE001
            logger.error("주문가능 조회 실패 %s(%s): %s — 스킵", name, ticker, exc)
            skipped.append((name, "orderable_failed"))
            notification_service.notify_error("매수 스킵(주문가능 조회 실패)", f"{name}({ticker}): {exc}")
            continue
        budget = min(alloc, remaining, orderable["cash"])
        qty = int(budget // limit_price)
        if orderable["max_qty"]:
            qty = min(qty, orderable["max_qty"])
        if qty < 1:
            skipped.append((name, "insufficient_cash"))
            continue

        atr = _compute_atr_asof(ticker, today)     # buy_date(today) 기준 ATR
        tp, sl = compute_tp_sl(limit_price, atr)
        if tp is None or sl is None:
            # TP/SL 없는 진입은 다음 매도 사이클이 risk_unmanaged 로 즉시 청산 — 스프레드·
            # 수수료만 태우는 매수·매도 왕복이 된다. 보호 불가면 진입하지 않는다(fail-close §2.4).
            logger.warning("매수 스킵 %s(%s) — ATR 미산출로 TP/SL 설정 불가(fail-close)", name, ticker)
            skipped.append((name, "atr_unavailable"))
            notification_service.notify_warn(
                "매수 스킵(ATR 미산출)", f"{name}({ticker}) — 익절/손절 계산 불가로 진입하지 않음."
            )
            continue

        # 1) 주문 의도 먼저 INSERT(buy_ordered) — 주문 송신 전 기록(§2.7).
        #    DB 유니크 제약 위반(동시성/중복) 시 주문 보내지 말고 스킵(중복주문 방지).
        try:
            rec_id = trade_repo.insert_record({
                "env": env, "ticker": ticker, "name": name, "status": "buy_ordered",
                "quantity": qty, "buy_price": limit_price, "buy_date": today,
                "buy_ordered_at": datetime.now(KST).isoformat(),
                "take_profit": tp, "stop_loss": sl, "atr": atr,
            })
        except trade_repo.DuplicatePositionError:
            logger.warning("중복 포지션 차단(DB 유니크) %s(%s) — 주문 스킵", name, ticker)
            skipped.append((name, "duplicate_open"))
            continue
        # 2) 주문 송신 — 재시도 금지.
        body = {
            "CANO": settings.kis_cano, "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
            "PDNO": ticker, "ORD_DVSN": "00", "ORD_QTY": str(qty), "ORD_UNPR": str(limit_price),
        }
        try:
            resp = kis_client.post_order(_ORDER_PATH, _tr("TTTC0802U", "VTTC0802U"), body)
            odno = (resp.get("output") or {}).get("ODNO")
            trade_repo.update_record(rec_id, {"buy_order_id": odno})
            remaining -= qty * limit_price
            dedup.add(ticker)
            ordered.append({"ticker": ticker, "name": name, "qty": qty, "price": limit_price,
                            "tp": tp, "sl": sl})
        except kis_client.KisOrderUncertainError as exc:
            # 체결여부 불명 — buy_ordered 유지(재주문 금지). reconcile 이 잔고로 확정:
            # 체결이면 holding(실체결가 반영), 미체결이면 다음 영업일 buy_expired.
            logger.error("매수 주문 체결여부 불명 %s(%s): %s — buy_ordered 유지, reconcile 대기", name, ticker, exc)
            dedup.add(ticker)
            skipped.append((name, "order_uncertain"))
            notification_service.notify_error(
                "매수 주문 체결여부 불명", f"{name}({ticker}) — 재주문 금지, reconcile 이 잔고로 확정합니다."
            )
        except kis_client.KisError as exc:
            logger.error("매수 주문 실패 %s(%s): %s", name, ticker, exc)
            trade_repo.update_record(rec_id, {"status": "buy_failed"})
            skipped.append((name, "order_failed"))
            notification_service.notify_error("매수 주문 실패", f"{name}({ticker}): {exc}")

    logger.info("매수 실행 %s — 주문 %d종목, 스킵 %d", rec_date, len(ordered), len(skipped))
    if ordered:
        def _won(v: float | None) -> str:
            return f"{v:,.0f}" if v is not None else "백필예정"
        lines = "\n".join(
            f"• {o['name']}({o['ticker']}) {o['qty']}주 @ {_won(o['price'])} — 익절 {_won(o['tp'])} / 손절 {_won(o['sl'])}"
            for o in ordered
        )
        notification_service.notify_info(f"매수 주문 {len(ordered)}종목 ({rec_date})", lines)
    elif skipped:
        # BUY 결정은 있었는데 주문 0건 — 침묵하면 매수 잡 미실행/크래시와 구분 불가(warn).
        reasons = "\n".join(f"• {n}: {r}" for n, r in skipped)
        notification_service.notify_warn(f"매수 주문 0건 ({rec_date}) — 전 종목 스킵", reasons)
    return {"date": rec_date, "ordered": len(ordered), "orders": ordered,
            "skipped": skipped, "status": "ok"}


# ──────────────────────────────────────────────────────────────────────────
#  매도 (장중 1분 주기)
# ──────────────────────────────────────────────────────────────────────────
def _vix_t1(today: str) -> float | None:
    """직전 영업일 VIX — scoring_service.vix_gate 재사용(look-ahead 차단·임계 일관).

    결측/ffill 정체(block_reason)면 그 VIX 값은 불량 → None 반환(매도 VIX 가중 스킵).
    불량 데이터로 과청산하느니 순수 기술신호 임계만 적용(보수적).
    """
    try:
        _regime, vix, reason = scoring_service.vix_gate(today)
        if reason in ("vix_missing", "vix_stall") or vix is None:
            return None  # 데이터 불량 → VIX 가중 스킵(과청산 방지). vix_block(고VIX)은 신뢰 → 값 반환.
        return vix
    except Exception as exc:  # noqa: BLE001
        logger.warning("VIX[T-1] 조회 실패(매도 가중 생략): %s", exc)
        return None


def _market_sell(rec: dict, reason: str) -> bool:
    """보유 포지션 시장가 청산. 재시도 금지.

    주문 송신 '전' holding→sell_ordered CAS 선점 — 스케줄 잡과 수동 /trade/sell-cycle
    호출(또는 이중 기동)이 같은 포지션을 동시에 팔려 할 때 한쪽만 통과(§2.7 이중 매도
    차단, 매수의 '주문 전 INSERT+유니크'와 대칭). 확정 거부 시 holding 으로 되돌려
    다음 사이클이 재점검한다.
    """
    if not trade_repo.claim_transition(rec["id"], "holding",
                                       {"status": "sell_ordered", "sell_reason": reason}):
        logger.info("매도 선점 실패 %s(%s) — 다른 실행이 이미 처리 중(중복 주문 방지)",
                    rec.get("name"), rec["ticker"])
        return False
    body = {
        "CANO": settings.kis_cano, "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
        "PDNO": rec["ticker"], "ORD_DVSN": "01",  # 시장가
        "ORD_QTY": str(rec["quantity"]), "ORD_UNPR": "0",
    }
    try:
        resp = kis_client.post_order(_ORDER_PATH, _tr("TTTC0801U", "VTTC0801U"), body)
        odno = (resp.get("output") or {}).get("ODNO")
        trade_repo.update_record(rec["id"], {"sell_order_id": odno})
        logger.info("매도 주문(시장가) %s(%s) 사유=%s", rec.get("name"), rec["ticker"], reason)
        notification_service.notify_info(
            "매도 주문(시장가)",
            f"{rec.get('name')}({rec['ticker']}) {rec.get('quantity')}주 — 사유: {reason}",
        )
        return True
    except kis_client.KisOrderUncertainError as exc:
        # 체결여부 불명 — sell_ordered 유지(이미 선점 전이됨)로 재전송(이중 매도)을 막는다.
        # reconcile 이 잔고로 확정: 체결이면 sold, 미체결이면 다음 영업일 holding 복귀.
        logger.error("매도 주문 체결여부 불명 %s(%s): %s — sell_ordered 유지, reconcile 대기",
                     rec.get("name"), rec["ticker"], exc)
        notification_service.notify_error(
            "매도 주문 체결여부 불명",
            f"{rec.get('name')}({rec['ticker']}) 사유={reason} — 이중 매도 방지를 위해 오늘은 자동 재주문하지 "
            "않습니다. 체결되면 reconcile 이 매도 완료로 확정하고, 미체결이면 다음 영업일에 감시를 재개합니다. "
            "손절 사유라면 KIS 잔고를 직접 확인해 주세요.",
        )
        return False
    except kis_client.KisError as exc:
        # 확정 거부 — 주문이 안 나갔으므로 holding 복귀(다음 사이클 재시도 가능).
        logger.error("매도 주문 실패 %s(%s): %s", rec.get("name"), rec["ticker"], exc)
        trade_repo.update_record(rec["id"], {"status": "holding"})
        notification_service.notify_error(
            "매도 주문 실패", f"{rec.get('name')}({rec['ticker']}) 사유={reason}: {exc}"
        )
        return False


# 장중 reconcile 최소 간격 제어용 단조시계 타임스탬프(기동 직후 첫 사이클은 즉시 실행).
_last_reconcile_mono: float = 0.0

# 매도 기술신호용 확정 일봉 캐시 — (ticker, today) → DataFrame | None.
# 확정봉(오늘 미만)만 쓰므로 하루 안에는 불변 → 1분 루프의 원격 조회를 종목당 1회로 줄인다
# (390사이클 × N종목 스크레이핑 → FDR 차단·사이클 지연 방지). 날짜가 바뀌면 통째로 버린다.
_daily_bars_cache: dict[tuple[str, str], pd.DataFrame | None] = {}


def _confirmed_daily_bars(ticker: str, today: str) -> pd.DataFrame | None:
    """오늘 미만(확정분) 일봉 — 하루 1회만 원격 조회하고 캐시."""
    key = (ticker, today)
    if key not in _daily_bars_cache:
        # 날짜가 바뀌면 이전 날짜 캐시는 전부 무효 — 메모리 누수 방지 위해 비운다.
        for old_key in [k for k in _daily_bars_cache if k[1] != today]:
            _daily_bars_cache.pop(old_key, None)
        start = (pd.Timestamp(today) - pd.Timedelta(days=_HIST_DAYS)).strftime("%Y-%m-%d")
        daily = _fetch_ohlcv_safe(ticker, start, today)
        if daily is not None:
            # look-ahead 차단: 장중 호출 시 당일 미확정봉이 섞일 수 있으므로 '오늘 미만'만 사용.
            daily = daily[daily.index < pd.Timestamp(today)]
        _daily_bars_cache[key] = daily
    return _daily_bars_cache[key]


def run_sell_cycle() -> dict:
    """보유 종목 매도 점검(장중 1분 주기 호출). TP/SL·기술신호·VIX·time-stop·ATR 백필.

    무인 운영 가드: 휴장일/장외 시간에는 아무것도 하지 않는다(정지 시세로 패닉셀 오판정 방지).
    시작 시 reconcile 을 먼저 돌려 buy_ordered→holding 전이를 장중에 반영 —
    매수 당일 체결 포지션도 같은 날부터 TP/SL 감시 대상에 들어간다.
    """
    env = settings.kis_environment
    now = _now_kst()
    today = now.strftime("%Y-%m-%d")

    if not market_calendar.is_market_hours(now):
        return {"checked": 0, "sold": 0, "status": "market_closed"}

    # 체결 전이를 장중에 반영(실패해도 감시는 계속 — 전이 없인 기존 holding 만 점검).
    # 매 사이클(60초) 잔고 조회는 모의서버 부하·레이트리밋의 주요인(2026-09 운영 관측)이라
    # 최소 간격을 두고 호출한다. 실패해도 다음 허용 시점까지 재시도하지 않는다(부하 억제).
    global _last_reconcile_mono
    if time.monotonic() - _last_reconcile_mono >= settings.SELL_RECONCILE_INTERVAL_SEC:
        _last_reconcile_mono = time.monotonic()
        try:
            reconcile()
        except Exception as exc:  # noqa: BLE001
            logger.warning("장중 reconcile 실패(감시는 계속, %d초 후 재시도): %s",
                           settings.SELL_RECONCILE_INTERVAL_SEC, exc)

    holdings = trade_repo.fetch_holdings(env)
    if not holdings:
        return {"checked": 0, "sold": 0, "status": "no_holdings"}

    vix = _vix_t1(today)
    # 수급: 최신 스냅샷이 stale(기준일 격차 초과)이면 매도 가속에 쓰지 않는다(오래된 수급으로 오청산 방지).
    snapshot = flow_repo.fetch_latest_snapshot()
    flow_by_ticker: dict[str, float | None] = {}
    if snapshot:
        calc_date = snapshot[0].get("calculation_date")
        gap = _busdays(calc_date, today) if calc_date else 999
        if gap <= settings.ASOF_MAX_GAP_DAYS:
            flow_by_ticker = {r["ticker"]: r.get("flow_score") for r in snapshot}
        else:
            logger.warning("수급 스냅샷 stale(%s, %d영업일) — 매도 flow 가중 생략", calc_date, gap)
    sold = 0
    errors: list[str] = []
    # 종목별 감시 상태(스케줄러의 '감시 공백' 알림용). price = 이번 사이클에 받은 유효 현재가
    # (시세 실패 시 None), error = 실패 원인(예외 타입명 또는 invalid_price, 정상이면 None).
    positions: dict[str, dict] = {}
    for rec in holdings:
        ticker = rec["ticker"]
        positions[ticker] = {"name": rec.get("name"), "stop_loss": rec.get("stop_loss"),
                             "price": None, "error": None}
        # 종목별 예외 격리 — 한 종목의 시세/DB 오류가 나머지 보유 종목의 손절/익절 감시를
        # 막으면 안 된다(손절 감시는 최종 안전망). 실패 종목만 스킵하고 다음 사이클 재시도.
        try:
            # ── ATR 백필(buy_date 기준) ──
            if rec.get("take_profit") is None or rec.get("stop_loss") is None:
                atr = _compute_atr_asof(ticker, rec["buy_date"]) if rec.get("buy_date") else None
                tp, sl = compute_tp_sl(rec.get("buy_price") or 0, atr)
                if tp is None:
                    trade_repo.update_record(rec["id"], {"risk_unmanaged": True})
                    logger.warning("ATR 백필 실패 %s — risk_unmanaged(차기 시장가 청산 후보)", ticker)
                    if _market_sell(rec, "risk_unmanaged"):  # 리스크 미관리 → 즉시 시장가 청산
                        sold += 1
                    continue
                trade_repo.update_record(rec["id"], {"take_profit": tp, "stop_loss": sl, "atr": atr,
                                                     "risk_unmanaged": False})
                rec["take_profit"], rec["stop_loss"] = tp, sl
                positions[ticker]["stop_loss"] = sl

            quote = balance_service.fetch_price(ticker, quick=True)
            price = quote["price"]
            if price <= 0:
                # 빈 응답(rt_cd 누락 + output 없음) 등 — 손절 판정을 못 했으니 '확인 실패'로 기록해야
                # 감시 공백 추적이 끊기지 않는다(성공으로 치면 연속 실패 시계가 리셋됨).
                logger.warning("매도 점검 스킵 %s(%s) — 유효하지 않은 현재가 %s", rec.get("name"), ticker, price)
                positions[ticker]["error"] = "invalid_price"
                errors.append(ticker)
                continue
            positions[ticker]["price"] = price
            sl_hit = price <= rec["stop_loss"]
            tp_hit = price >= rec["take_profit"]
            time_stop = bool(rec.get("buy_date")) and _busdays(rec["buy_date"], today) >= settings.TIME_STOP_DAYS

            # 확정 일봉은 하루 1회 캐시 — 1분 루프에서는 원격 조회 없이 신호 재평가만.
            daily = _confirmed_daily_bars(ticker, today)
            sig = (evaluate_sell_signals(daily, quote, flow_by_ticker.get(ticker))
                   if daily is not None and len(daily) > settings.SMA_LONG else {"count": 0})

            should, reason = decide_sell(
                sl_hit=sl_hit, tp_hit=tp_hit, time_stop=time_stop,
                signal_count=sig["count"], flow_score=flow_by_ticker.get(ticker), vix_t1=vix,
            )
            if should and _market_sell(rec, reason):
                sold += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("매도 점검 실패 %s(%s): %s — 이 종목만 스킵(다음 사이클 재시도)",
                         rec.get("name"), ticker, exc)
            # 시세는 받았는데 그 뒤(DB 선점·일봉 등)에서 실패했을 수도 있다 — 알림이 원인을 구분하도록 기록.
            positions[ticker]["error"] = type(exc).__name__
            errors.append(ticker)

    logger.info("매도 사이클 — 점검 %d, 매도주문 %d, 오류 %d (VIX[T-1]=%s)",
                len(holdings), sold, len(errors), vix)
    return {"checked": len(holdings), "sold": sold, "errors": errors,
            "positions": positions, "vix_t1": vix, "status": "ok"}


# ──────────────────────────────────────────────────────────────────────────
#  정합성 동기화
# ──────────────────────────────────────────────────────────────────────────
def _kst_date_of(ts_value) -> str | None:
    """DB TIMESTAMPTZ(ISO 문자열) → KST 날짜 문자열. 파싱 실패 시 None."""
    if not ts_value:
        return None
    try:
        ts = pd.Timestamp(ts_value)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
        return ts.tz_convert(KST).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return None


def reconcile() -> dict:
    """KIS 잔고 ↔ trade_records 동기화 — 상태기계의 모든 탈출구를 처리한다.

    전이 목록:
    - buy_ordered & 보유    → holding (+실체결가 pchs_avg_pric 로 buy_price/TP/SL 재계산)
    - buy_ordered & 미보유  → buy_date 가 지났으면 buy_expired (미체결 좀비 청소 — KIS 는
                              장 마감 시 미체결 주문 자동 취소). 당일이면 체결 대기로 유지.
    - sell_ordered & 미보유 → sold (체결가는 ccld 연동 전까지 현재가 근사 — 보정 사용 금지)
    - sell_ordered & 보유   → **주문일이 오늘이면 유지**(원 주문이 호가창에 살아있을 수 있어
                              당일 되돌림 = 이중 매도 위험). 전일 이전 주문만 holding 복귀
                              (KIS 가 장 마감에 자동 취소했으므로 재시도 안전).
    - holding & 미보유      → sold (외부/미기록 청산 — 매분 매도 재시도 루프 차단)
    - holding & 보유        → 수량 불일치 시 재동기화(부분체결·수동 개입 반영)
    - KIS 보유 & DB 없음    → 고아 INSERT(holding)
    """
    env = settings.kis_environment
    bal = balance_service.fetch_balance()
    kis_holdings = bal["holdings"]            # {ticker: qty}
    # 실체결 평균단가(pchs_avg_pric) — 매수 체결 전이 시 TP/SL 앵커 보정용.
    avg_price_by_ticker: dict[str, float] = {}
    for row in bal.get("raw_holdings") or []:
        code = row.get("pdno")
        try:
            avg = float(row.get("pchs_avg_pric") or 0)
        except (TypeError, ValueError):
            avg = 0.0
        if code and avg > 0:
            avg_price_by_ticker[code] = avg

    open_pos = trade_repo.fetch_open_positions(env)
    db_tickers = {r["ticker"] for r in open_pos}
    today = _now_kst().strftime("%Y-%m-%d")
    transitions = []

    for rec in open_pos:
        t = rec["ticker"]
        if rec["status"] == "buy_ordered" and t in kis_holdings:
            # 매수 체결 → holding. 실체결가로 buy_price 를 갱신하고, 그 앵커로 TP/SL 재계산
            # (주문가 앵커 유지 시 갭 체결에서 손절폭이 왜곡됨). ATR 은 buy_date 기준값 유지(§2.4).
            fields: dict = {"status": "holding", "quantity": kis_holdings[t]}
            fill_price = avg_price_by_ticker.get(t)
            if fill_price and fill_price > 0:
                fields["buy_price"] = fill_price
                tp, sl = compute_tp_sl(fill_price, rec.get("atr"))
                if tp is not None:
                    fields["take_profit"], fields["stop_loss"] = tp, sl
            trade_repo.update_record(rec["id"], fields)
            transitions.append((t, "buy_ordered→holding"))
            # ATR 미산출(백필 대기) 포지션은 TP/SL 이 None — 0원으로 표기하면 실제 임계로
            # 오독되므로 '백필예정'으로 구분(execute_buys 의 _won 표기와 동일 규칙).
            tp_val = fields.get("take_profit", rec.get("take_profit"))
            sl_val = fields.get("stop_loss", rec.get("stop_loss"))
            tp_txt = f"{tp_val:,.0f}" if tp_val is not None else "백필예정"
            sl_txt = f"{sl_val:,.0f}" if sl_val is not None else "백필예정"
            notification_service.notify_info(
                "매수 체결 확인",
                f"{rec.get('name')}({t}) {kis_holdings[t]}주"
                + (f" @ {fill_price:,.0f}" if fill_price else "")
                + f" — 익절 {tp_txt} / 손절 {sl_txt}",
            )
        elif rec["status"] == "buy_ordered" and t not in kis_holdings:
            # 미체결 좀비 청소: 주문일이 지났으면 KIS 가 이미 자동 취소한 주문 → 종결.
            # (당일은 체결 대기 중일 수 있으므로 유지.)
            if rec.get("buy_date") and rec["buy_date"] < today:
                trade_repo.update_record(rec["id"], {"status": "buy_expired"})
                transitions.append((t, "buy_ordered→buy_expired(미체결 만료)"))
                notification_service.notify_warn(
                    "매수 미체결 만료", f"{rec.get('name')}({t}) — 주문일 {rec['buy_date']}, 재매수 가능 상태로 종결."
                )
        elif rec["status"] == "sell_ordered" and t not in kis_holdings:
            # 매도 체결 → sold. ⚠ 체결가/손익은 체결조회(ccld) 연동 전까지 현재가 근사 →
            #    슬리피지로 실제와 다를 수 있음. **이 근사 손익으로 배수/임계 보정 금지**(stock-quant).
            quote = balance_service.fetch_price(t)
            sell_price = quote["price"]
            buy_price = rec.get("buy_price") or 0
            qty = rec.get("quantity") or 0
            pl = (sell_price - buy_price) * qty if buy_price else None
            pl_pct = ((sell_price / buy_price - 1) * 100) if buy_price else None
            trade_repo.update_record(rec["id"], {
                "status": "sold", "sell_price": sell_price, "sell_date": today,
                "profit_loss": pl, "profit_loss_pct": round(pl_pct, 4) if pl_pct is not None else None,
            })
            transitions.append((t, "sell_ordered→sold"))
            notification_service.notify_info(
                "매도 체결 확인",
                f"{rec.get('name')}({t}) {qty}주 — 사유: {rec.get('sell_reason')}, "
                f"근사 손익 {pl:,.0f}원({pl_pct:+.2f}%)" if pl is not None else
                f"{rec.get('name')}({t}) {qty}주 — 사유: {rec.get('sell_reason')}",
            )
        elif rec["status"] == "sell_ordered" and t in kis_holdings:
            # 미체결(거래정지·하한가·VI 등). **당일 주문은 유지** — 원 주문이 살아있는 상태에서
            # holding 복귀 → 재주문은 이중 매도(§2.7 위반). 전일 이전 주문만 복귀(장 마감 자동취소 후).
            ordered_date = _kst_date_of(rec.get("updated_at"))
            if ordered_date is None or ordered_date < today:
                trade_repo.update_record(rec["id"], {"status": "holding"})
                transitions.append((t, "sell_ordered→holding(전일 미체결 재시도)"))
            else:
                logger.info("매도 미체결 유지 %s — 당일 주문(이중 매도 방지), 다음 영업일 재시도", t)
        elif rec["status"] == "holding" and t not in kis_holdings:
            # DB 는 보유인데 잔고엔 없음 = 수동 매도·타임아웃 뒤 실체결 등 미기록 청산.
            # 방치 시 sell_cycle 이 매분 매도 재시도(잔고부족 거부) 무한 루프 → 종결.
            trade_repo.update_record(rec["id"], {
                "status": "sold", "sell_date": today,
                "sell_reason": rec.get("sell_reason") or "closed_external",
            })
            transitions.append((t, "holding→sold(외부 청산, 체결가 미상)"))
            notification_service.notify_warn(
                "외부 청산 감지", f"{rec.get('name')}({t}) — KIS 잔고에 없어 sold 로 종결(체결가/손익 미상)."
            )
        elif rec["status"] == "holding" and t in kis_holdings:
            # 부분체결·수동 개입으로 수량이 어긋나면 잔고 기준으로 재동기화(매도 수량 오류 방지).
            if rec.get("quantity") != kis_holdings[t]:
                trade_repo.update_record(rec["id"], {"quantity": kis_holdings[t]})
                transitions.append((t, f"quantity {rec.get('quantity')}→{kis_holdings[t]} 재동기화"))

    # KIS엔 있는데 DB 미청산에 없는 고아 종목 → INSERT(holding). 유니크 제약으로 중복 INSERT 방지.
    for t, qty in kis_holdings.items():
        if t not in db_tickers:
            try:
                trade_repo.insert_record({"env": env, "ticker": t, "status": "holding", "quantity": qty})
                transitions.append((t, "orphan→holding"))
            except trade_repo.DuplicatePositionError:
                logger.info("고아 INSERT 스킵(이미 존재) %s", t)

    if transitions:
        logger.info("정합성 동기화 — 전이 %d건: %s", len(transitions), transitions)
    return {"transitions": transitions, "status": "ok"}
