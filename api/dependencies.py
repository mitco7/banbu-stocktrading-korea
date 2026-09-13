"""API 공용 의존성(Dependency Injection) 정의.

FastAPI 의 Depends 로 라우트에 주입할 공용 자원(설정, 인증 등)을 모은다.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from core.config import Settings, get_settings


def settings_dep() -> Settings:
    """요청 핸들러에 Settings 를 주입하기 위한 의존성."""
    return get_settings()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """X-API-Key 헤더 검증(§2.6 — 매매 API 무단 호출 방지).

    - API_AUTH_KEY 가 설정돼 있으면: 헤더가 정확히 일치해야 통과(상수시간 비교).
    - 비어 있으면: 인증 비활성(로컬 개발 전용 — 기동 시 경고 로그).
      AWS 등 외부 배포에서는 반드시 설정 + 보안그룹 차단을 병행한다.
    """
    expected = get_settings().API_AUTH_KEY
    if not expected:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="유효하지 않은 API 키(X-API-Key 헤더 필요)")
