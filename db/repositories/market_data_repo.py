"""market_data 테이블 CRUD (멱등 벌크 upsert).

멱등성(CLAUDE.md §2.7): 같은 날짜로 두 번 돌려도 안전해야 한다.
  - 이미 있는 날짜: **현재 NULL 인 컬럼만** 신규 비결측 값으로 채운다(기존 값 덮어쓰지 않음).
  - 없는 날짜: insert.

이 규칙은 Postgres 함수 `upsert_market_data_fill_empty`(sql/003)에서 컬럼별 COALESCE 로
**한 문장에 원자적으로** 처리한다(read-modify-write 경합·행당 round-trip 제거).
"""

from __future__ import annotations

from core.market_spec import DATE_COL
from db.client import get_supabase
from utils.logger import get_logger

logger = get_logger(__name__)

TABLE = "market_data"
_RPC = "upsert_market_data_fill_empty"
_RPC_CHUNK = 1000  # 한 번에 보낼 행 수(요청 페이로드 크기 제한).


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def upsert_fill_empty(records: list[dict]) -> dict[str, int]:
    """records 를 멱등 규칙으로 벌크 저장. 각 record 는 'date' 키 + 데이터 컬럼.

    Returns: {"upserted": 처리한 행 수}
    """
    if not records:
        return {"upserted": 0}

    client = get_supabase()
    upserted = 0
    for chunk in _chunks(records, _RPC_CHUNK):
        client.rpc(_RPC, {"rows": chunk}).execute()
        upserted += len(chunk)
    return {"upserted": upserted}


def count_rows() -> int:
    client = get_supabase()
    resp = client.table(TABLE).select(DATE_COL, count="exact").limit(1).execute()
    return resp.count or 0


def fetch_last_row() -> dict | None:
    """가장 최근 날짜의 전체 행(증분 수집의 완성도 판단용). 없으면 None."""
    client = get_supabase()
    resp = (
        client.table(TABLE).select("*").order(DATE_COL, desc=True).limit(1).execute()
    )
    return resp.data[0] if resp.data else None


def date_range() -> tuple[str | None, str | None]:
    """저장된 데이터의 (min, max) 날짜."""
    client = get_supabase()
    lo = client.table(TABLE).select(DATE_COL).order(DATE_COL, desc=False).limit(1).execute()
    hi = client.table(TABLE).select(DATE_COL).order(DATE_COL, desc=True).limit(1).execute()
    lo_d = lo.data[0][DATE_COL] if lo.data else None
    hi_d = hi.data[0][DATE_COL] if hi.data else None
    return lo_d, hi_d


def fetch_series_before(column: str, before_date: str, limit: int = 15) -> list[dict]:
    """`column` 시계열을 `before_date` **이전**(미만, 당일 제외)으로 최근 `limit`개 조회.

    날짜 오름차순 [{날짜, <column>}] 반환. VIX[T-1] 등 look-ahead 차단용으로 당일은 제외한다
    (한국 아침에 알 수 있는 미국 VIX 는 전날 종가뿐 — 당일 행을 읽으면 누수).
    """
    client = get_supabase()
    resp = (
        client.table(TABLE)
        .select(f'{DATE_COL},"{column}"')
        .lt(DATE_COL, before_date)
        .order(DATE_COL, desc=True)
        .limit(limit)
        .execute()
    )
    rows = resp.data or []
    return list(reversed(rows))  # 오름차순으로
