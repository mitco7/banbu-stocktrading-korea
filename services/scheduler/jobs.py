"""APScheduler 기반 일일 매매 스케줄 (Step 7 — 무인 운영의 심장).

잡 구성(시각·주기는 전부 core/config.py 로 외부화, KST):
  1. daily_pipeline   07:00       — 데이터 수집 → ML → 매수 결정 (/pipeline/run 과 동일 로직)
                                    (06:00 이전 금지 — 미국장 마감 경합, config 주석 참조)
  2. execute_buys     10:00       — 장 시작 1시간 후 BUY 결정 집행
  3. sell_cycle       60초 주기   — 장중 TP/SL·기술신호·time-stop 감시(장외엔 내부 가드 즉시 반환)
  4. reconcile_close  15:40       — 장 마감 후 체결 정합성 동기화(마감 직전 체결 알림 지연 해소)
  + startup_catchup   기동 +15초  — 재시작으로 놓친 당일 파이프라인 1회 만회(아래 참조)

원칙:
- 잡 함수는 서비스 함수를 그대로 호출한다(로직 중복 금지). 휴장일·주문 시간창·중복 주문 방지·
  fail-close 등 안전 가드는 전부 서비스 레이어에 있고, 스케줄러는 시각만 책임진다.
- 잡 예외는 스케줄러를 죽이지 않는다: 로그 + Slack notify(쿨다운으로 스팸 방지).
- max_instances=1 + coalesce=True — 같은 잡의 스케줄러 내 중복 실행 금지. 수동 API 호출과의
  프로세스 간 경합은 서비스 레이어의 DB 가드(매수 INSERT+유니크, 매도 CAS 선점)가 차단(§2.7).
- 운영은 단일 프로세스 전제(uvicorn workers=1, run.py 주석 참조).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core import market_calendar
from core.config import settings
from db.repositories import decision_repo, run_repo
from services import notification_service, pipeline_service, trading_service
from utils.logger import get_logger

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")

_scheduler: BackgroundScheduler | None = None
_last_error_notify: dict[str, float] = {}  # 알림 키 → monotonic 시각(쿨다운)


def _now() -> datetime:
    """현재 KST — 테스트에서 monkeypatch 로 고정할 수 있게 분리."""
    return datetime.now(KST)


def _parse_hhmm(value: str) -> tuple[int, int]:
    """"HH:MM" → (hour, minute). 형식 오류는 기동 시점에 즉시 실패(fail fast)."""
    h, m = value.strip().split(":")
    hour, minute = int(h), int(m)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"잘못된 스케줄 시각: {value!r} (KST HH:MM)")
    return hour, minute


def _notify_throttled(key: str, title: str, message: str = "", *, level: str = "error") -> bool:
    """쿨다운 걸린 Slack 알림 — 반복 실패(1분 주기 잡 등)의 스팸을 묶는다.

    Returns:
        실제 발송했으면 True(호출부가 누적 카운터를 리셋할 수 있게), 쿨다운 억제면 False.
    """
    now = time.monotonic()
    last = _last_error_notify.get(key)
    if last is not None and now - last < settings.SCHEDULE_ERROR_NOTIFY_COOLDOWN_SEC:
        return False
    _last_error_notify[key] = now
    send = notification_service.notify_warn if level == "warn" else notification_service.notify_error
    send(title, message)
    return True


def _notify_job_error(job_id: str, exc: Exception) -> None:
    _notify_throttled(job_id, f"스케줄 잡 실패 — {job_id}", f"{type(exc).__name__}: {exc}")


def _wrap(job_id: str, fn: Callable[[], None]) -> Callable[[], None]:
    """잡 예외 격리 래퍼 — 한 잡의 실패가 스케줄러/다른 잡에 영향 주지 않게(로그 + 알림만)."""

    def _runner() -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — 무인 운영: 어떤 예외도 삼키고 알린다
            logger.exception("스케줄 잡 실패 — %s", job_id)
            _notify_job_error(job_id, exc)

    return _runner


def _in_window(now_t: dtime, start_hhmm: str, end_hhmm: str) -> bool:
    sh, sm = _parse_hhmm(start_hhmm)
    eh, em = _parse_hhmm(end_hhmm)
    return dtime(sh, sm) <= now_t <= dtime(eh, em)


# ──────────────────────────────────────────────────────────────────────────
#  잡 본체
# ──────────────────────────────────────────────────────────────────────────
def job_daily_pipeline() -> None:
    """일일 파이프라인(수집→ML→결정). 휴장일은 스킵 — Kaggle GPU·LLM 헛호출 방지.

    force_market 기본 False: 새벽엔 당일 시세가 없어 True 면 전일값 ffill 로 가짜 당일 행이
    저장되고 이후 실제 종가가 못 덮어쓴다(config 의 SCHEDULE_PIPELINE_FORCE_MARKET 주석 참조).
    """
    now = _now()
    if not market_calendar.is_trading_day(now):
        logger.info("파이프라인 잡 스킵 — 휴장일(%s)", now.strftime("%Y-%m-%d"))
        return
    logger.info("스케줄: 일일 파이프라인 시작(force_market=%s)",
                settings.SCHEDULE_PIPELINE_FORCE_MARKET)
    result = pipeline_service.run_daily_pipeline(
        force_market=settings.SCHEDULE_PIPELINE_FORCE_MARKET, skip_ml=False
    )
    logger.info("스케줄: 일일 파이프라인 종료 — status=%s, 매수 %s종목",
                result.get("status"), (result.get("buy") or {}).get("count", 0))
    if result.get("status") != "ok":
        return
    # 지각 만회: 매수 잡 시각(10:00)이 이미 지났고 아직 주문 시간창 내면 여기서 1회 집행
    # (기동 만회·수동 재실행으로 파이프라인이 늦게 성공한 날). 정상 07:00 실행에선 조건
    # 불성립 — 10:00 크론이 담당. execute_buys 내부 가드가 휴장일·시간창·stale·중복을
    # 전부 재검증하므로 중복 호출돼도 안전하다(§2.7).
    now_t = _now().timetz().replace(tzinfo=None)
    if _in_window(now_t, settings.SCHEDULE_BUY_TIME, settings.BUY_ORDER_END):
        logger.info("파이프라인 지각 완료 — 매수 잡 시각 경과, 지금 매수 집행")
        job_execute_buys()


def job_execute_buys() -> None:
    """BUY 결정 집행. 휴장일·시간창·결정 신선도·중복 방지 가드는 execute_buys 내부에 있다."""
    result = trading_service.execute_buys()
    logger.info("스케줄: 매수 잡 — status=%s, 주문 %s건",
                result.get("status"), result.get("ordered"))


# 매도 감시 공백 추적(종목별). 사이클 단위 일시 오류는 모의서버에서 흔해(2026-09 운영 관측)
# 건별로 알리면 소음이 된다 — 한 종목의 점검이 SELL_STALE_ALERT_MIN 이상 연속 실패할 때만
# warn 1건, 복구되면 info 1건. 단, 손절선 아래인데 매도 처리가 실패하면 기다리지 않고 즉시 error.
_fail_since: dict[str, float] = {}      # 연속 실패 시작(monotonic 초)
_gap_alerted: set[str] = set()          # 공백 warn 발송 완료
_sl_fail_alerted: set[str] = set()      # '손절선 도달·매도 실패' error 발송 완료
_last_price: dict[str, float] = {}      # 마지막으로 받은 유효 현재가
_last_error: dict[str, str] = {}        # 마지막 실패 원인(예외 타입명 / invalid_price)
_names: dict[str, str] = {}             # 종목명(장 마감 알림용 — 그 시점엔 사이클 결과가 없다)


def _mono() -> float:
    return time.monotonic()


def _position_line(ticker: str, pos: dict, head: str) -> str:
    name = pos.get("name") or ticker
    last, sl = _last_price.get(ticker), pos.get("stop_loss")
    if last and sl:
        tail = f"마지막 확인가 {last:,.0f}원 · 손절선 {sl:,.0f}원까지 {(last / sl - 1) * 100:+.1f}%"
    else:
        tail = "오늘 확인된 가격 없음"
    return f"• {name}({ticker}) — {head} · {tail}"


def _failure_kind(ticker: str) -> str:
    """시세 자체를 못 받았는지, 시세는 받았는데 매도 처리 단계가 실패했는지 구분."""
    err = _last_error.get(ticker)
    if err == "invalid_price":
        return "현재가 응답 비정상"
    if err in (None, "KisError"):
        return "시세 확인 불가(KIS 응답 지연·실패)"
    return f"매도 처리 오류({err})"


def _reset_sell_tracking() -> None:
    for tracked in (_fail_since, _gap_alerted, _sl_fail_alerted, _last_price, _last_error, _names):
        tracked.clear()


def job_sell_cycle() -> None:
    """장중 매도 감시(TP/SL·기술신호·time-stop). 장외엔 내부 가드가 즉시 반환하므로 상시 주기 안전."""
    result = trading_service.run_sell_cycle()
    if result.get("status") != "ok":
        # 공백인 채로 장이 끝났으면 1건 알린다(복구 알림이 영영 안 오는 상태를 남기지 않도록).
        if result.get("status") == "market_closed" and _gap_alerted:
            notification_service.notify_info(
                "장 마감 — 감시 공백 상태로 마감한 종목",
                "\n".join(f"• {_names.get(t) or t}({t})" for t in sorted(_gap_alerted))
                + "\n오늘 장 마감까지 점검이 복구되지 않았습니다. 다음 장 시작 후 다시 감시합니다.",
            )
        # 장외·보유 없음 — 감시가 끝났으니 추적도 초기화(다음 장 첫 실패를 전날부터로 오산 방지).
        _reset_sell_tracking()
        return

    now = _mono()
    positions: dict[str, dict] = result.get("positions") or {}
    errors = set(result.get("errors") or [])
    for tracked in (_fail_since, _last_price, _last_error, _names):  # 보유에서 빠진 종목(청산 등)은 추적 종료
        for t in [t for t in tracked if t not in positions]:
            tracked.pop(t)
    _gap_alerted.intersection_update(positions)
    _sl_fail_alerted.intersection_update(positions)

    recovered: list[str] = []
    urgent: list[str] = []
    for t, pos in positions.items():
        price, sl = pos.get("price"), pos.get("stop_loss")
        if pos.get("name"):
            _names[t] = pos["name"]
        if price:
            _last_price[t] = price  # 시세를 받은 뒤 실패한 경우도 최신가는 유지
        if t in errors:
            _fail_since.setdefault(t, now)
            _last_error[t] = pos.get("error") or "KisError"
            if price and sl and price <= sl and t not in _sl_fail_alerted:
                urgent.append(t)
            continue
        started = _fail_since.pop(t, None)
        _last_error.pop(t, None)
        _sl_fail_alerted.discard(t)
        if t in _gap_alerted:
            _gap_alerted.discard(t)
            gap = f"약 {(now - started) / 60:.0f}분 만에 " if started is not None else ""
            recovered.append(f"• {pos.get('name') or t}({t}) — {gap}점검 재개")

    if urgent and notification_service.notify_error(
        f"손절선 도달 — 매도 처리 실패 {len(urgent)}종목",
        "\n".join(_position_line(t, positions[t], f"매도 처리 오류({_last_error[t]})") for t in urgent)
        + "\n1분마다 자동 재시도 중입니다. 바로 해소되지 않으면 KIS 잔고를 직접 확인해 주세요.",
    ):
        _sl_fail_alerted.update(urgent)

    threshold = settings.SELL_STALE_ALERT_MIN * 60
    stale = sorted(t for t, since in _fail_since.items()
                   if t not in _gap_alerted and now - since >= threshold)
    if stale and notification_service.notify_warn(
        f"매도 감시 공백 — {len(stale)}종목 {settings.SELL_STALE_ALERT_MIN}분 이상 점검 실패",
        "\n".join(_position_line(t, positions[t],
                                 f"{(now - _fail_since[t]) / 60:.0f}분째 {_failure_kind(t)}")
                  for t in stale)
        + "\n이 종목의 익절·손절 판단이 멈춰 있습니다. 1분마다 자동 재시도 중이며, 복구되면 다시 알려드립니다.",
    ):
        _gap_alerted.update(stale)  # 발송 실패 시 다음 사이클에 다시 시도
    if recovered:
        notification_service.notify_info("매도 감시 정상화", "\n".join(recovered))


def job_reconcile_close() -> None:
    """장 마감 후 정합성 동기화 — 마감 직전 체결의 '체결 확인' 알림이 다음 영업일로 밀리지 않게."""
    now = _now()
    if not market_calendar.is_trading_day(now):
        return
    result = trading_service.reconcile()
    logger.info("스케줄: 장마감 reconcile — 전이 %d건", len(result.get("transitions") or []))


def job_startup_catchup() -> None:
    """기동 만회 — MemoryJobStore 는 프로세스 다운 중 놓친 크론을 재현하지 못한다.

    거래일 + 파이프라인 시각 경과 + 매수 마감(BUY_ORDER_END) 전 + 오늘 결정 없음이면
    일일 파이프라인을 1회 만회 실행한다(성공 시 job_daily_pipeline 이 매수까지 연계).
    개발 reload 환경에서 원치 않으면 SCHEDULER_ENABLED=false 로 끌 것.
    """
    now = _now()
    if not market_calendar.is_trading_day(now):
        return
    now_t = now.timetz().replace(tzinfo=None)
    if not _in_window(now_t, settings.SCHEDULE_PIPELINE_TIME, settings.BUY_ORDER_END):
        return
    today = now.strftime("%Y-%m-%d")
    # 1차 판정: pipeline_runs — 후보 0건 날은 decision_logs 가 비므로 결정 로그만 보면
    # 재시작마다 파이프라인(Kaggle·LLM 포함)을 재실행하는 루프가 된다. 실행 기록(running/ok)
    # 이 있으면 만회 불필요. running 체크는 07:00 크론과의 기동 경합(이중 실행) 창도 막는다.
    if run_repo.has_run_today("daily_pipeline", today):
        logger.info("기동 만회 불필요 — 오늘 파이프라인 실행 기록 존재(pipeline_runs)")
        return
    # 2차 판정(pipeline_runs 조회 실패 대비): 오늘 결정 로그가 있으면 완주한 것.
    if decision_repo.latest_date() == today:
        logger.info("기동 만회 불필요 — 오늘 결정 로그 존재")
        return
    logger.info("기동 만회 — 오늘 파이프라인 미실행 감지, 지금 1회 실행")
    notification_service.notify_warn(
        "파이프라인 기동 만회 실행",
        f"{now.strftime('%H:%M')} 기동 시점에 오늘 매수 결정이 없어 일일 파이프라인을 지금 실행합니다.",
    )
    job_daily_pipeline()


# ──────────────────────────────────────────────────────────────────────────
#  스케줄러 생명주기
# ──────────────────────────────────────────────────────────────────────────
def create_scheduler() -> BackgroundScheduler:
    """주기 잡 4종을 등록한 BackgroundScheduler 생성(시작은 하지 않음 — 테스트에서 검사 가능).

    BackgroundScheduler(스레드 실행)인 이유: 파이프라인 잡이 ML 대기까지 최대 15분 블로킹이라
    AsyncIOScheduler 로 이벤트 루프에서 돌리면 API 응답이 통째로 멎는다.
    """
    sched = BackgroundScheduler(timezone=KST)
    ph, pm = _parse_hhmm(settings.SCHEDULE_PIPELINE_TIME)
    bh, bm = _parse_hhmm(settings.SCHEDULE_BUY_TIME)
    rh, rm = _parse_hhmm(settings.SCHEDULE_RECONCILE_TIME)
    common = {"max_instances": 1, "coalesce": True}
    # misfire_grace_time 은 '프로세스 생존 중 지연'(절전·VM 일시정지·실행기 포화)만 만회한다.
    # MemoryJobStore 라 프로세스 다운 중 놓친 크론은 재현되지 않는다 — 재시작 만회는
    # start() 가 등록하는 startup_catchup 잡이 담당.
    sched.add_job(_wrap("daily_pipeline", job_daily_pipeline),
                  CronTrigger(day_of_week="mon-fri", hour=ph, minute=pm, timezone=KST),
                  id="daily_pipeline", misfire_grace_time=3600, **common)
    sched.add_job(_wrap("execute_buys", job_execute_buys),
                  CronTrigger(day_of_week="mon-fri", hour=bh, minute=bm, timezone=KST),
                  id="execute_buys", misfire_grace_time=3600, **common)
    sched.add_job(_wrap("sell_cycle", job_sell_cycle),
                  IntervalTrigger(seconds=settings.SCHEDULE_SELL_INTERVAL_SEC, timezone=KST),
                  id="sell_cycle", misfire_grace_time=30, **common)
    sched.add_job(_wrap("reconcile_close", job_reconcile_close),
                  CronTrigger(day_of_week="mon-fri", hour=rh, minute=rm, timezone=KST),
                  id="reconcile_close", misfire_grace_time=3600, **common)
    return sched


def start() -> None:
    """스케줄러 기동(중복 호출 안전 — 이미 실행 중이면 no-op) + 기동 만회 1회 예약."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        logger.info("스케줄러 이미 실행 중 — start() 무시")
        return
    _scheduler = create_scheduler()
    # 기동 15초 뒤 만회 검사 1회(앱 기동을 막지 않도록 잡으로 위임 — 실패해도 _wrap 이 격리).
    _scheduler.add_job(_wrap("startup_catchup", job_startup_catchup), "date",
                       run_date=_now() + timedelta(seconds=15),
                       id="startup_catchup", misfire_grace_time=300)
    _scheduler.start()
    logger.info(
        "스케줄러 시작(KST) — pipeline %s, buy %s, sell %d초 주기, reconcile %s",
        settings.SCHEDULE_PIPELINE_TIME, settings.SCHEDULE_BUY_TIME,
        settings.SCHEDULE_SELL_INTERVAL_SEC, settings.SCHEDULE_RECONCILE_TIME,
    )


def shutdown() -> None:
    """스케줄러 종료. wait=False 로 lifespan 반환은 즉시지만, CPython 은 인터프리터 종료
    단계에서 실행 중 잡 스레드를 join 하므로 프로세스 종료는 잡이 끝날 때까지(최대 15분
    파이프라인) 지연될 수 있다. systemd 사용 시 TimeoutStopSec 를 짧게 두면 SIGKILL 로
    끊기는데, 잡이 전부 멱등이라 강제 종료돼도 데이터 사고는 없다(재기동 시 catchup 만회).
    """
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("스케줄러 종료")
