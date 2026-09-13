"""KIS 접근토큰 관리 (Step 6).

조회 순서: 메모리 캐시 → DB(access_tokens) → 만료/부재 시 /oauth2/tokenP 신규 발급.
KIS 토큰 발급은 **1분당 1회 제한** → threading.Lock 으로 직렬화. Lock 안에서 캐시를 재확인해
(double-checked locking) 동시 요청이 중복 발급하지 않게 한다. 토큰은 ~24h 유효.

⚠ 토큰은 시크릿 — 로그에 값 출력 금지(앞 일부만/마스킹).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.config import settings
from db.repositories import token_repo
from services import kis_client
from utils.logger import get_logger

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")
# 만료 이 초 이내면 미리 갱신(경계에서 만료된 토큰으로 주문하는 사고 방지).
_REFRESH_MARGIN_SEC = 600

_lock = threading.Lock()
# 메모리 캐시: {env: (token, expires_at_datetime)}
_cache: dict[str, tuple[str, datetime]] = {}


def _now() -> datetime:
    return datetime.now(KST)


def _valid(expires_at: datetime) -> bool:
    return expires_at - _now() > timedelta(seconds=_REFRESH_MARGIN_SEC)


def _parse_dt(value: str) -> datetime:
    """ISO 문자열 → KST aware datetime."""
    ts = datetime.fromisoformat(value)
    return ts.astimezone(KST) if ts.tzinfo else ts.replace(tzinfo=KST)


def get_access_token() -> str:
    """현재 환경(PAPER/PROD)의 유효한 KIS 접근토큰 반환. 필요 시 발급/갱신."""
    env = settings.kis_environment

    # 1) 메모리 캐시.
    cached = _cache.get(env)
    if cached and _valid(cached[1]):
        return cached[0]

    with _lock:
        # 2) Lock 재진입 후 캐시 재확인(다른 스레드가 방금 갱신했을 수 있음).
        cached = _cache.get(env)
        if cached and _valid(cached[1]):
            return cached[0]

        # 3) DB 캐시(테이블 부재/DB 장애 시에도 발급으로 진행 — 토큰 조회가 하드실패하지 않게).
        try:
            row = token_repo.fetch_token(env)
        except Exception as exc:  # noqa: BLE001
            logger.warning("토큰 DB 조회 실패(발급으로 진행): %s", exc)
            row = None
        if row and row.get("access_token") and row.get("expires_at"):
            exp = _parse_dt(row["expires_at"])
            if _valid(exp):
                _cache[env] = (row["access_token"], exp)
                logger.info("KIS 토큰 DB 캐시 재사용(env=%s, 만료=%s)", env, exp.isoformat())
                return row["access_token"]

        # 4) 신규 발급.
        if not settings.kis_appkey or not settings.kis_appsecret:
            raise kis_client.KisError(f"KIS 자격증명 미설정(env={env}). .env 의 KIS_{env}_APPKEY 등을 확인하세요.")
        data = kis_client.request_access_token()
        token = data["access_token"]
        expires_in = int(data.get("expires_in", 86400))
        exp = _now() + timedelta(seconds=expires_in)
        _cache[env] = (token, exp)
        try:
            token_repo.upsert_token(env, token, exp.isoformat(), data.get("token_type", "Bearer"))
        except Exception as exc:  # noqa: BLE001 — DB 저장 실패해도 메모리 토큰으로 진행
            logger.warning("토큰 DB 저장 실패(메모리 캐시로 진행): %s", exc)
        logger.info("KIS 토큰 신규 발급(env=%s, 만료=%s)", env, exp.isoformat())
        return token


def reset_cache() -> None:
    """메모리 토큰 캐시 비우기(테스트/강제갱신용)."""
    _cache.clear()
