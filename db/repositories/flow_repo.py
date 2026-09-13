"""investor_flow 테이블 CRUD (수급 점수 스냅샷).

종목별 최신 스냅샷 1행만 유지(ticker PK). 매 실행 시 덮어쓰기(upsert).
market_data 의 '빈 컬럼만 채우는' 멱등과 달리, 수급 스냅샷은 최신값으로 전체 갱신한다.
"""

from __future__ import annotations

from db.client import get_supabase
from utils.logger import get_logger

logger = get_logger(__name__)

TABLE = "investor_flow"


def upsert_flows(records: list[dict]) -> int:
    """수급 레코드를 (ticker, calculation_date) 기준 upsert. 처리한 행 수 반환.

    같은 기준일 재실행은 갱신(멱등), 새 기준일은 새 행으로 이력 보존.
    """
    if not records:
        return 0
    client = get_supabase()
    client.table(TABLE).upsert(records, on_conflict="ticker,calculation_date").execute()
    return len(records)


def latest_calculation_date() -> str | None:
    client = get_supabase()
    resp = (
        client.table(TABLE)
        .select("calculation_date")
        .order("calculation_date", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0]["calculation_date"] if resp.data else None


def fetch_latest_snapshot() -> list[dict]:
    """가장 최근 기준일의 종목별 수급 점수(현재 스냅샷)."""
    calc_date = latest_calculation_date()
    if calc_date is None:
        return []
    client = get_supabase()
    resp = (
        client.table(TABLE)
        .select("*")
        .eq("calculation_date", calc_date)
        .order("ticker")
        .execute()
    )
    return resp.data or []
