"""decision_logs CRUD (일자별 최종 매매 의사결정 로그).

(decision_date, ticker) 복합키로 일자별 이력 보존. 같은 날 재실행은 그 날짜 행 삭제 후 재삽입.
"""

from __future__ import annotations

from db.client import get_supabase
from utils.logger import get_logger

logger = get_logger(__name__)

TABLE = "decision_logs"


def replace_for_date(date: str, records: list[dict]) -> int:
    """해당 날짜 결정 로그를 (decision_date, ticker) 기준 upsert. 저장 행 수 반환.

    delete-후-insert 대신 단일 upsert 로 **원자적** 처리한다 — delete 성공 후 insert 실패로
    그날 결정이 통째로 증발하는 사고 방지(§2.7). 후보 집합은 고정 25유니버스에서 결정적이라
    같은 날 재실행 시 동일 ticker 집합이 들어와 고아 행이 남지 않는다.
    """
    if not records:
        return 0
    client = get_supabase()
    client.table(TABLE).upsert(records, on_conflict="decision_date,ticker").execute()
    return len(records)


def fetch_by_date(date: str) -> list[dict]:
    """특정 날짜의 결정 로그(정렬 순위 오름차순)."""
    client = get_supabase()
    resp = (
        client.table(TABLE)
        .select("*")
        .eq("decision_date", date)
        .order("rank", desc=False)
        .execute()
    )
    return resp.data or []


def latest_date() -> str | None:
    client = get_supabase()
    resp = (
        client.table(TABLE)
        .select("decision_date")
        .order("decision_date", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0]["decision_date"] if resp.data else None
