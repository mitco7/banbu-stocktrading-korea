"""stock_recommendations CRUD (일자별 종목 분석 스냅샷 + 매수 후보 표시).

(날짜, ticker) 복합키로 **일자별 이력 보존**(다른 날짜는 그대로 둠). 같은 날 재실행은
**그 날짜 행을 전부 삭제 후 재삽입**해 매 실행 통째 갱신한다(predict.py 패턴 — 고아 행 방지).
"""

from __future__ import annotations

from db.client import get_supabase
from utils.logger import get_logger

logger = get_logger(__name__)

TABLE = "stock_recommendations"


def delete_by_date(date: str) -> None:
    """특정 날짜의 모든 행 삭제."""
    client = get_supabase()
    client.table(TABLE).delete().eq("날짜", date).execute()


def replace_for_date(date: str, records: list[dict]) -> int:
    """해당 날짜 행을 모두 삭제한 뒤 재삽입(매 실행 통째 갱신). 저장한 행 수 반환.

    같은 날 재실행 시 이전 실행 잔재(유니버스 교체로 빠진 종목 등)를 남기지 않는다.
    ⚠ delete→insert 가 원자적이지 않아 두 호출 사이 짧은 순간 그 날짜가 빈 상태가 될 수 있다
       (조회 동시성이 중요해지면 RPC 단일 트랜잭션으로 승격 고려).
    """
    if not records:
        return 0
    client = get_supabase()
    client.table(TABLE).delete().eq("날짜", date).execute()
    client.table(TABLE).insert(records).execute()
    return len(records)


def fetch_by_date(date: str) -> list[dict]:
    """특정 날짜의 추천 후보 목록(예측 상승률 내림차순)."""
    client = get_supabase()
    resp = (
        client.table(TABLE)
        .select("*")
        .eq("날짜", date)
        .order("predicted_rise_pct", desc=True)
        .execute()
    )
    return resp.data or []


def latest_date() -> str | None:
    """가장 최근 추천 산출일."""
    client = get_supabase()
    resp = (
        client.table(TABLE).select("날짜").order("날짜", desc=True).limit(1).execute()
    )
    return resp.data[0]["날짜"] if resp.data else None
