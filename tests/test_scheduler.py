"""스케줄러 검증 — 잡 등록(시각·주기·중복방지 옵션), 예외 격리, 휴장일 스킵, 에러 알림 쿨다운."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.config import settings
from services.scheduler import jobs

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture(autouse=True)
def _reset_error_cooldown():
    """테스트 간 에러 알림 쿨다운·감시 공백 추적 상태 격리."""
    def clear():
        jobs._last_error_notify.clear()
        jobs._reset_sell_tracking()
    clear()
    yield
    clear()


# ── 잡 등록 ────────────────────────────────────────────────────────────────
def test_create_scheduler_registers_all_jobs():
    sched = jobs.create_scheduler()
    sched.start(paused=True)  # 잡 실행 없이 잡스토어에 반영만
    try:
        by_id = {j.id: j for j in sched.get_jobs()}
        assert set(by_id) == {"daily_pipeline", "execute_buys", "sell_cycle", "reconcile_close"}

        # cron 시각이 config 값과 일치(KST).
        def cron_fields(job) -> dict[str, str]:
            return {f.name: str(f) for f in job.trigger.fields}

        ph, pm = jobs._parse_hhmm(settings.SCHEDULE_PIPELINE_TIME)
        assert cron_fields(by_id["daily_pipeline"])["hour"] == str(ph)
        assert cron_fields(by_id["daily_pipeline"])["minute"] == str(pm)
        bh, bm = jobs._parse_hhmm(settings.SCHEDULE_BUY_TIME)
        assert cron_fields(by_id["execute_buys"])["hour"] == str(bh)
        assert cron_fields(by_id["execute_buys"])["minute"] == str(bm)
        rh, rm = jobs._parse_hhmm(settings.SCHEDULE_RECONCILE_TIME)
        assert cron_fields(by_id["reconcile_close"])["hour"] == str(rh)
        assert cron_fields(by_id["reconcile_close"])["minute"] == str(rm)

        # 매도 감시는 인터벌 트리거 + 설정 주기.
        sell = by_id["sell_cycle"]
        assert sell.trigger.interval.total_seconds() == settings.SCHEDULE_SELL_INTERVAL_SEC

        # 중복 실행 금지 옵션(§2.7).
        for job in by_id.values():
            assert job.max_instances == 1
            assert job.coalesce is True
    finally:
        sched.shutdown(wait=False)


def test_parse_hhmm():
    assert jobs._parse_hhmm("06:00") == (6, 0)
    assert jobs._parse_hhmm("15:40") == (15, 40)
    with pytest.raises(ValueError):
        jobs._parse_hhmm("24:00")
    with pytest.raises(ValueError):
        jobs._parse_hhmm("bad")


# ── 예외 격리 + 에러 알림 쿨다운 ───────────────────────────────────────────
def test_wrap_swallows_exception_and_notifies_once(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(jobs.notification_service, "notify_error",
                        lambda title, msg="": calls.append(title) or True)

    def boom() -> None:
        raise RuntimeError("의도된 실패")

    runner = jobs._wrap("sell_cycle", boom)
    runner()  # 예외가 밖으로 새면 테스트 실패
    runner()  # 쿨다운 안 — 두 번째 알림은 억제
    assert calls == ["스케줄 잡 실패 — sell_cycle"]


def _sell_result(errors: list[str], prices: dict[str, float | None]) -> dict:
    """run_sell_cycle 결과 모사 — 보유 종목별 name/stop_loss/price(실패 종목은 None)."""
    return {"status": "ok", "errors": errors,
            "positions": {t: {"name": f"종목{t[-1]}", "stop_loss": 90_000.0, "price": p}
                          for t, p in prices.items()}}


@pytest.fixture
def sell_gap(monkeypatch):
    """매도 감시 공백 알림 테스트 하네스 — 시계·사이클 결과·Slack 발송을 조작/기록."""
    state = {"now": 0.0, "result": {}, "warns": [], "infos": [], "errors": [], "slack_ok": True}
    monkeypatch.setattr(jobs, "_mono", lambda: state["now"])
    monkeypatch.setattr(jobs.trading_service, "run_sell_cycle", lambda: state["result"])

    def recorder(key):
        return lambda t, m="": state[key].append((t, m)) or state["slack_ok"]
    monkeypatch.setattr(jobs.notification_service, "notify_warn", recorder("warns"))
    monkeypatch.setattr(jobs.notification_service, "notify_info", recorder("infos"))
    monkeypatch.setattr(jobs.notification_service, "notify_error", recorder("errors"))

    def cycle(minute: float, errors: list[str], prices: dict[str, float | None]) -> None:
        state["now"] = minute * 60
        state["result"] = _sell_result(errors, prices)
        jobs.job_sell_cycle()
    state["cycle"] = cycle
    return state


def test_sell_gap_alerts_once_after_threshold_then_recovers(sell_gap):
    """사이클 단위 일시 실패는 조용히, 임계 이상 연속 실패는 warn 1건, 복구 시 info 1건."""
    limit = settings.SELL_STALE_ALERT_MIN
    cycle = sell_gap["cycle"]
    cycle(0, [], {"000001": 100_000.0, "000002": 95_000.0})          # 정상 — 마지막 확인가 기록
    cycle(1, ["000001"], {"000001": None, "000002": 95_000.0})        # 실패 시작
    cycle(limit, ["000001"], {"000001": None, "000002": 95_000.0})    # 임계 직전(limit-1분 경과)
    assert sell_gap["warns"] == []
    cycle(limit + 1, ["000001"], {"000001": None, "000002": 95_000.0})  # 임계 도달 → warn 1건
    assert len(sell_gap["warns"]) == 1
    title, body = sell_gap["warns"][0]
    assert "1종목" in title and "종목1(000001)" in body
    assert "시세 확인 불가" in body                                     # 원인 구분(시세 단계 실패)
    assert "마지막 확인가 100,000원" in body and "+11.1%" in body     # 손절선 90,000 대비 여유
    cycle(limit + 3, ["000001"], {"000001": None, "000002": 95_000.0})  # 계속 실패 — 재알림 없음
    assert len(sell_gap["warns"]) == 1
    cycle(limit + 4, [], {"000001": 101_000.0, "000002": 95_000.0})   # 복구 → info 1건
    assert len(sell_gap["infos"]) == 1 and "000001" in sell_gap["infos"][0][1]
    assert not jobs._fail_since and not jobs._gap_alerted


def test_sell_gap_intermittent_failures_stay_silent(sell_gap):
    """실패와 성공이 번갈아 나오면(9/18 모의서버 패턴) 연속 실패가 임계에 못 미쳐 알림 없음."""
    for minute in range(0, 30):
        failing = ["000001"] if minute % 2 else []
        sell_gap["cycle"](minute, failing, {"000001": None if failing else 100_000.0})
    assert sell_gap["warns"] == [] and sell_gap["infos"] == []


def test_sell_gap_state_resets_after_market_close(sell_gap):
    """장 마감(market_closed) 후엔 추적 초기화 — 다음 날 첫 실패를 전날부터 이어진 공백으로 오산하지 않는다."""
    sell_gap["cycle"](0, ["000001"], {"000001": None})
    sell_gap["result"] = {"status": "market_closed", "checked": 0, "sold": 0}
    jobs.job_sell_cycle()
    assert not jobs._fail_since
    sell_gap["cycle"](18 * 60, ["000001"], {"000001": None})  # 다음 날 장 시작 첫 실패
    assert sell_gap["warns"] == []


def test_sell_gap_drops_positions_no_longer_held(sell_gap):
    """청산 등으로 보유에서 빠진 종목은 추적을 끝낸다(없는 종목에 공백 알림 금지)."""
    limit = settings.SELL_STALE_ALERT_MIN
    sell_gap["cycle"](0, ["000001"], {"000001": None, "000002": 95_000.0})
    sell_gap["cycle"](limit + 1, [], {"000002": 95_000.0})  # 000001 매도 완료로 보유 목록에서 빠짐
    assert sell_gap["warns"] == [] and "000001" not in jobs._fail_since


def test_stop_loss_hit_but_sell_processing_fails_alerts_immediately(sell_gap):
    """시세는 받았고 손절선 아래인데 그 뒤(DB 선점 등)가 실패 — 5분 기다리지 말고 즉시 error 1건."""
    sell_gap["result"] = _sell_result(["000001"], {"000001": 85_000.0})
    sell_gap["result"]["positions"]["000001"]["error"] = "APIError"
    jobs.job_sell_cycle()
    assert len(sell_gap["errors"]) == 1
    title, body = sell_gap["errors"][0]
    assert "손절선 도달" in title and "매도 처리 오류(APIError)" in body and "85,000원" in body
    jobs.job_sell_cycle()                                   # 같은 실패 반복 — 재알림 없음
    assert len(sell_gap["errors"]) == 1
    sell_gap["cycle"](1, [], {"000001": 85_000.0})          # 정상 처리되면 추적 해제
    assert "000001" not in jobs._sl_fail_alerted


def test_gap_alert_retries_when_slack_send_fails(sell_gap):
    """Slack 발송이 실패하면 '알림 완료'로 표시하지 않고 다음 사이클에 다시 보낸다."""
    limit = settings.SELL_STALE_ALERT_MIN
    sell_gap["cycle"](0, ["000001"], {"000001": None})
    sell_gap["slack_ok"] = False
    sell_gap["cycle"](limit, ["000001"], {"000001": None})
    assert len(sell_gap["warns"]) == 1 and not jobs._gap_alerted
    sell_gap["slack_ok"] = True
    sell_gap["cycle"](limit + 1, ["000001"], {"000001": None})
    assert len(sell_gap["warns"]) == 2 and jobs._gap_alerted == {"000001"}


def test_market_close_reports_unresolved_gap(sell_gap):
    """공백 경고 후 복구 없이 장이 끝나면 마감 알림 1건(복구 알림이 영영 안 오는 상태 방지)."""
    limit = settings.SELL_STALE_ALERT_MIN
    sell_gap["cycle"](0, ["000001"], {"000001": None})
    sell_gap["cycle"](limit, ["000001"], {"000001": None})
    sell_gap["result"] = {"status": "market_closed", "checked": 0, "sold": 0}
    jobs.job_sell_cycle()
    jobs.job_sell_cycle()                                   # 장외 반복 호출 — 마감 알림은 1건만
    closing = [m for t, m in sell_gap["infos"] if t.startswith("장 마감")]
    assert len(closing) == 1 and "종목1(000001)" in closing[0]


def test_wrap_notifies_again_after_cooldown(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(jobs.notification_service, "notify_error",
                        lambda title, msg="": calls.append(title) or True)
    monkeypatch.setattr(settings, "SCHEDULE_ERROR_NOTIFY_COOLDOWN_SEC", 0)

    def boom() -> None:
        raise RuntimeError("의도된 실패")

    runner = jobs._wrap("daily_pipeline", boom)
    runner()
    runner()
    assert len(calls) == 2


# ── 휴장일 가드 / 지각 매수 연계 ──────────────────────────────────────────
def _fake_now(monkeypatch, dt: datetime) -> None:
    monkeypatch.setattr(jobs, "_now", lambda: dt)


def test_pipeline_job_skips_on_holiday(monkeypatch):
    monkeypatch.setattr(jobs.market_calendar, "is_trading_day", lambda now: False)
    monkeypatch.setattr(jobs.pipeline_service, "run_daily_pipeline",
                        lambda **kw: pytest.fail("휴장일엔 파이프라인을 호출하면 안 됨"))
    jobs.job_daily_pipeline()  # 호출 없이 조용히 반환해야 함


def test_pipeline_job_runs_on_trading_day_without_force(monkeypatch):
    """거래일엔 실행하되 force_market 은 설정 기본값(False) — 새벽 ffill 오염 방지."""
    _fake_now(monkeypatch, datetime(2026, 6, 12, 7, 10, tzinfo=KST))  # 금요일 07:10(매수 연계 전)
    monkeypatch.setattr(jobs.market_calendar, "is_trading_day", lambda now: True)
    monkeypatch.setattr(jobs, "job_execute_buys",
                        lambda: pytest.fail("새벽 정상 실행에선 매수 연계가 없어야 함"))
    received: dict = {}

    def fake_pipeline(**kwargs) -> dict:
        received.update(kwargs)
        return {"status": "ok", "buy": {"count": 0}}

    monkeypatch.setattr(jobs.pipeline_service, "run_daily_pipeline", fake_pipeline)
    jobs.job_daily_pipeline()
    assert received == {"force_market": False, "skip_ml": False}


def test_pipeline_job_triggers_buy_when_late(monkeypatch):
    """파이프라인이 매수 잡 시각 이후(주문창 내) 완료되면 매수를 즉시 1회 연계 집행."""
    _fake_now(monkeypatch, datetime(2026, 6, 12, 11, 0, tzinfo=KST))  # 금요일 11:00
    monkeypatch.setattr(jobs.market_calendar, "is_trading_day", lambda now: True)
    monkeypatch.setattr(jobs.pipeline_service, "run_daily_pipeline",
                        lambda **kw: {"status": "ok", "buy": {"count": 1}})
    buys: list[int] = []
    monkeypatch.setattr(jobs, "job_execute_buys", lambda: buys.append(1))
    jobs.job_daily_pipeline()
    assert buys == [1]


def test_pipeline_job_no_buy_link_on_failure(monkeypatch):
    """파이프라인 실패 시(fail-close) 매수 연계 금지."""
    _fake_now(monkeypatch, datetime(2026, 6, 12, 11, 0, tzinfo=KST))
    monkeypatch.setattr(jobs.market_calendar, "is_trading_day", lambda now: True)
    monkeypatch.setattr(jobs.pipeline_service, "run_daily_pipeline",
                        lambda **kw: {"status": "failed", "buy": None})
    monkeypatch.setattr(jobs, "job_execute_buys",
                        lambda: pytest.fail("실패한 파이프라인 뒤에 매수하면 안 됨"))
    jobs.job_daily_pipeline()


# ── 기동 만회(startup catchup) ────────────────────────────────────────────
def test_startup_catchup_runs_when_no_decisions(monkeypatch):
    _fake_now(monkeypatch, datetime(2026, 6, 12, 11, 0, tzinfo=KST))  # 거래일 11:00
    monkeypatch.setattr(jobs.market_calendar, "is_trading_day", lambda now: True)
    monkeypatch.setattr(jobs.run_repo, "has_run_today", lambda job, d: False)
    monkeypatch.setattr(jobs.decision_repo, "latest_date", lambda: "2026-06-11")  # 어제 것뿐
    monkeypatch.setattr(jobs.notification_service, "notify_warn", lambda *a, **k: True)
    called: list[int] = []
    monkeypatch.setattr(jobs, "job_daily_pipeline", lambda: called.append(1))
    jobs.job_startup_catchup()
    assert called == [1]


def test_startup_catchup_skips_when_already_done(monkeypatch):
    _fake_now(monkeypatch, datetime(2026, 6, 12, 11, 0, tzinfo=KST))
    monkeypatch.setattr(jobs.market_calendar, "is_trading_day", lambda now: True)
    monkeypatch.setattr(jobs.run_repo, "has_run_today", lambda job, d: False)
    monkeypatch.setattr(jobs.decision_repo, "latest_date", lambda: "2026-06-12")  # 오늘 결정 존재
    monkeypatch.setattr(jobs, "job_daily_pipeline",
                        lambda: pytest.fail("오늘 결정이 있으면 만회 실행 금지"))
    jobs.job_startup_catchup()


def test_startup_catchup_skips_when_run_recorded(monkeypatch):
    """후보 0건 날(decision_logs 빈 날)에도 pipeline_runs 기록이 있으면 재실행 금지 —
    재시작마다 Kaggle·LLM 파이프라인을 반복 실행하는 루프 방지."""
    _fake_now(monkeypatch, datetime(2026, 6, 12, 11, 0, tzinfo=KST))
    monkeypatch.setattr(jobs.market_calendar, "is_trading_day", lambda now: True)
    monkeypatch.setattr(jobs.run_repo, "has_run_today", lambda job, d: True)  # 오늘 실행 기록 존재
    monkeypatch.setattr(jobs.decision_repo, "latest_date",
                        lambda: pytest.fail("run 기록이 있으면 결정 조회 전에 반환해야 함"))
    monkeypatch.setattr(jobs, "job_daily_pipeline",
                        lambda: pytest.fail("실행 기록이 있으면 만회 재실행 금지"))
    jobs.job_startup_catchup()


def test_startup_catchup_skips_outside_window(monkeypatch):
    """매수 마감(BUY_ORDER_END) 이후 기동은 만회 무의미 — 내일 크론이 정상 처리."""
    _fake_now(monkeypatch, datetime(2026, 6, 12, 16, 0, tzinfo=KST))
    monkeypatch.setattr(jobs.market_calendar, "is_trading_day", lambda now: True)
    monkeypatch.setattr(jobs.decision_repo, "latest_date",
                        lambda: pytest.fail("창 밖이면 DB 조회 전에 반환해야 함"))
    jobs.job_startup_catchup()


def test_reconcile_job_skips_on_holiday(monkeypatch):
    monkeypatch.setattr(jobs.market_calendar, "is_trading_day", lambda now: False)
    monkeypatch.setattr(jobs.trading_service, "reconcile",
                        lambda: pytest.fail("휴장일엔 reconcile 호출하면 안 됨"))
    jobs.job_reconcile_close()


# ── 생명주기 ───────────────────────────────────────────────────────────────
def test_start_is_idempotent_and_shutdown_safe(monkeypatch):
    try:
        jobs.start()
        first = jobs._scheduler
        jobs.start()  # 중복 호출 — 새 스케줄러를 만들면 안 됨
        assert jobs._scheduler is first
    finally:
        jobs.shutdown()
        assert jobs._scheduler is None
        jobs.shutdown()  # 미기동 상태에서도 안전
