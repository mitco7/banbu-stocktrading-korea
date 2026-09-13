"""Supabase 클라이언트 래퍼.

자격증명이 없어도 앱 기동은 가능해야 하므로(헬스체크/문서 등), 클라이언트는
지연 초기화(lazy)한다. 실제로 DB에 접근하는 시점에 get_supabase()를 호출하며,
이때 자격증명이 비어 있으면 명확한 에러로 실패시킨다.

이 모듈만 supabase SDK를 직접 사용한다. 다른 레이어는 db/repositories 를 통해 접근한다
(ARCHITECTURE.md 레이어 의존 규칙).
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from core.config import settings

if TYPE_CHECKING:
    from supabase import Client


@lru_cache
def get_supabase() -> "Client":
    """프로세스 전역 단일 Supabase 클라이언트(캐시).

    Raises:
        RuntimeError: SUPABASE_URL_KOREA / SUPABASE_KEY_KOREA 가 설정되지 않은 경우.
    """
    if not settings.SUPABASE_URL_KOREA or not settings.SUPABASE_KEY_KOREA:
        raise RuntimeError(
            "SUPABASE_URL_KOREA / SUPABASE_KEY_KOREA 가 설정되지 않았습니다. .env 를 확인하세요."
        )

    # import 를 함수 안으로 둬서, 자격증명 없이 앱을 띄울 때 SDK 의존을 강제하지 않는다.
    from supabase import create_client

    return create_client(settings.SUPABASE_URL_KOREA, settings.SUPABASE_KEY_KOREA)
