"""pipeline_runs 기록 (잡 실행 이력 — Step 7).

원칙: 기록은 **관측 수단이지 제어 흐름이 아니다** — 어떤 실패도 예외를 올리지 않는다.
(sql/012 미적용 등으로 테이블이 없어도 잡 자체는 계속 돌아야 한다. 경고 로그만 남긴다.)
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from db.client import get_supabase
from utils.logger import get_logger

logger = get_logger(__name__)

TABLE = "pipeline_runs"
KST = ZoneInfo("Asia/Seoul")


def _now_iso() -> str:
    return datetime.now(KST).isoformat()


def _safe_result(result: dict | None) -> dict | None:
    """result 를 JSON 직렬화 가능한 형태로 강제(비직렬화 객체는 문자열로)."""
    if result is None:
        return None
    try:
        return json.loads(json.dumps(result, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return {"repr": repr(result)[:2000]}


def start(job: str) -> int | None:
    """실행 시작 기록. 성공 시 run id, 실패 시 None(잡은 계속)."""
    try:
        client = get_supabase()
        resp = client.table(TABLE).insert({
            "job": job,
            "run_date": datetime.now(KST).strftime("%Y-%m-%d"),
            "status": "running",
            "started_at": _now_iso(),
        }).execute()
        return resp.data[0]["id"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline_runs 시작 기록 실패(무시 — sql/012 적용 여부 확인): %s", exc)
        return None


def has_run_today(job: str, run_date: str) -> bool:
    """해당 일자에 job 의 실행 기록(running/ok)이 있는지 — 기동 만회(catchup) 중복 방지용.

    - ok: 이미 완주(후보 0건으로 decision_logs 가 비는 날 포함) → 재실행 불필요.
    - running: 지금 다른 실행이 진행 중(기동 직후 크론과의 경합 창) → 중복 기동 방지.
    - failed 는 제외 → 실패한 날은 만회 재실행 허용(자동 복구).
    조회 실패 시 False(보수적으로 만회 진행 — 기록은 관측 수단이지 제어 흐름이 아님).
    """
    try:
        client = get_supabase()
        resp = (
            client.table(TABLE)
            .select("id")
            .eq("job", job)
            .eq("run_date", run_date)
            .in_("status", ["running", "ok"])
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline_runs 조회 실패(만회 판정은 decision_logs 기준으로 진행): %s", exc)
        return False


def finish(run_id: int | None, status: str, error: str | None = None,
           result: dict | None = None) -> None:
    """실행 종료 기록(status: ok/failed). run_id 가 None 이면 no-op."""
    if run_id is None:
        return
    try:
        client = get_supabase()
        client.table(TABLE).update({
            "status": status,
            "error": error,
            "result": _safe_result(result),
            "finished_at": _now_iso(),
        }).eq("id", run_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline_runs 종료 기록 실패(무시): %s", exc)
