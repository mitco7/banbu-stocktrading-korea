"""민감정보 마스킹 (CLAUDE.md §2.6).

로그/예외 메시지/응답 dump 에 시크릿(API 키)·계좌번호·토큰이 노출되지 않도록 가린다.
특히 ECOS 는 키를 URL 경로에, FRED 는 쿼리스트링에 넣으므로 requests 예외 메시지에 키가
그대로 들어간다 → 외부 호출 예외를 로깅하기 전 반드시 이 헬퍼로 마스킹할 것.
"""

from __future__ import annotations


def mask_secret(text: str, secret: str | None, keep: int = 4) -> str:
    """text 안의 secret 을 앞 keep 글자만 남기고 가린다."""
    if not secret or not text:
        return text
    masked = (secret[:keep] + "***") if len(secret) > keep else "***"
    return text.replace(secret, masked)


def mask_secrets(text: str, secrets: list[str | None]) -> str:
    """여러 시크릿을 한 번에 마스킹."""
    for s in secrets:
        text = mask_secret(text, s)
    return text


def mask_account(account: str | None, keep: int = 2) -> str:
    """계좌번호 등 식별자를 뒤 keep 자리만 남기고 가린다."""
    if not account:
        return ""
    if len(account) <= keep:
        return "*" * len(account)
    return "*" * (len(account) - keep) + account[-keep:]
