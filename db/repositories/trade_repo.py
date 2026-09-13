"""trade_records CRUD (포지션·체결 상태). env(PAPER/PROD)별 분리.

멱등성(§2.7): 주문 송신 전 INSERT(buy_ordered)로 의도를 먼저 기록. 중복매수 방지는
open_tickers(미청산 보유/주문중 집합)로 trading_service 가 검사한다.
"""

from __future__ import annotations

from db.client import get_supabase
from utils.logger import get_logger

logger = get_logger(__name__)

TABLE = "trade_records"
OPEN_STATUSES = ("buy_ordered", "holding", "sell_ordered")


class DuplicatePositionError(RuntimeError):
    """미청산 포지션 유니크 제약 위반 — 이미 주문/보유 중인 종목(중복주문 방지)."""


def insert_record(record: dict) -> int:
    """신규 행 INSERT 후 id 반환. 미청산 유니크 제약 위반 시 DuplicatePositionError.

    DB 유니크 인덱스(uq_trade_records_open_ticker)가 메모리 dedup 을 뚫은 동시성/중복을
    최종 차단한다. 호출자는 이 예외를 잡아 주문을 보내지 말고 스킵해야 한다(§2.7).
    """
    client = get_supabase()
    try:
        resp = client.table(TABLE).insert(record).execute()
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "duplicate" in msg or "unique" in msg or "23505" in msg or "uq_trade_records_open" in msg:
            raise DuplicatePositionError(
                f"{record.get('ticker')}: 이미 미청산 포지션 존재(중복주문 차단)"
            ) from None
        raise
    return resp.data[0]["id"]


def update_record(record_id: int, fields: dict) -> None:
    client = get_supabase()
    client.table(TABLE).update(fields).eq("id", record_id).execute()


def claim_transition(record_id: int, from_status: str, fields: dict) -> bool:
    """상태 조건부 갱신(CAS) — status 가 from_status 인 행만 갱신하고 성공 여부 반환.

    스케줄 잡과 수동 API 호출(또는 이중 기동)이 같은 포지션을 동시에 처리할 때
    한쪽만 통과시키는 가드(§2.7 중복 주문 차단). 갱신된 행이 0개 = 이미 다른 실행이
    전이함 → 호출자는 주문을 보내지 말고 스킵해야 한다.
    """
    client = get_supabase()
    resp = (
        client.table(TABLE)
        .update(fields)
        .eq("id", record_id)
        .eq("status", from_status)
        .execute()
    )
    return bool(resp.data)


def fetch_by_statuses(env: str, statuses: tuple[str, ...]) -> list[dict]:
    client = get_supabase()
    resp = (
        client.table(TABLE)
        .select("*")
        .eq("env", env)
        .in_("status", list(statuses))
        .execute()
    )
    return resp.data or []


def fetch_open_positions(env: str) -> list[dict]:
    """미청산(주문중/보유) 포지션."""
    return fetch_by_statuses(env, OPEN_STATUSES)


def open_tickers(env: str) -> set[str]:
    """중복매수 방지용 — 미청산 포지션의 종목코드 집합."""
    return {r["ticker"] for r in fetch_open_positions(env)}


def fetch_holdings(env: str) -> list[dict]:
    """매도 감시 대상 — 보유중 포지션."""
    return fetch_by_statuses(env, ("holding",))
