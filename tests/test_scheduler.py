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
    """테스트 간 에러 알림 쿨다운 상태 격리."""
    jobs._last_error_notify.clear()
    yield
    jobs._last_error_notify.clear()


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


def test_sell_cycle_error_summary_accumulates_and_resets(monkeypatch):
    """매도 감시 일시 오류는 건별 error 대신 누적 요약 warn — 발송 시 리셋, 쿨다운 내엔 누적만."""
    jobs._sell_err_total = 0
    jobs._sell_err_tickers.clear()
    warns, errs = [], []
    monkeypatch.setattr(jobs.notification_service, "notify_warn", lambda t, m="": warns.append(t) or True)
    monkeypatch.setattr(jobs.notification_service, "notify_error", lambda t, m="": errs.append(t) or True)
    monkeypatch.setattr(jobs.trading_service, "run_sell_cycle",
                        lambda: {"errors": ["005930", "000660"]})
    jobs.job_sell_cycle()  # 쿨다운 열림 → warn 1건 발송 + 누적 리셋
    assert warns == ["매도 감시 일시 오류 누적 2건 — 자동 재시도 중"]
    assert not errs
    assert jobs._sell_err_total == 0 and not jobs._sell_err_tickers

    monkeypatch.setattr(jobs.trading_service, "run_sell_cycle", lambda: {"errors": ["066570"]})
    jobs.job_sell_cycle()  # 쿨다운 내 → 발송 없이 누적만
    assert len(warns) == 1
    assert jobs._sell_err_total == 1 and jobs._sell_err_tickers == {"066570"}


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
