"""access_tokens CRUD (KIS 토큰 DB 캐시). env(PAPER/PROD)별 1행 upsert."""

from __future__ import annotations

from db.client import get_supabase
from utils.logger import get_logger

logger = get_logger(__name__)

TABLE = "access_tokens"


def fetch_token(env: str) -> dict | None:
    client = get_supabase()
    resp = client.table(TABLE).select("*").eq("env", env).limit(1).execute()
    return resp.data[0] if resp.data else None


def upsert_token(env: str, access_token: str, expires_at: str, token_type: str = "Bearer") -> None:
    client = get_supabase()
    client.table(TABLE).upsert(
        {"env": env, "access_token": access_token, "token_type": token_type, "expires_at": expires_at},
        on_conflict="env",
    ).execute()
