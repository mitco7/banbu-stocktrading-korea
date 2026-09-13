"""Slack 알림 발송 (Step 7 — 무인 운영의 관측 수단).

원칙:
- 알림은 **관측 수단일 뿐 제어 흐름이 아니다**: 발송 실패는 로그만 남기고 절대 예외를
  올리지 않는다(알림 장애가 파이프라인·매매를 멈추면 안 됨).
- 웹훅 URL 은 시크릿(§2.6) — 로그·예외 메시지 어디에도 URL 을 찍지 않는다.
- SLACK_WEBHOOK_URL 이 비어 있으면 전체 비활성(no-op). SLACK_NOTIFY_LEVEL 미만 레벨은 스킵.
"""

from __future__ import annotations

import requests

from core.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_TIMEOUT = 5.0
_LEVELS = {"info": 0, "warn": 1, "error": 2}
_EMOJI = {"info": ":white_check_mark:", "warn": ":warning:", "error": ":rotating_light:"}


def notify(level: str, title: str, message: str = "") -> bool:
    """Slack 웹훅으로 1건 발송. 성공 여부 반환(실패해도 예외 없음 — 로그만).

    level: info / warn / error. SLACK_NOTIFY_LEVEL 이상만 실제 발송.
    """
    url = settings.SLACK_WEBHOOK_URL
    if not url:
        return False
    lv = _LEVELS.get(level, 0)
    threshold = _LEVELS.get(settings.SLACK_NOTIFY_LEVEL.strip().lower(), 0)
    if lv < threshold:
        return False

    env_tag = settings.kis_environment  # PAPER/PROD — 어느 환경 알림인지 항상 표기
    text = f"{_EMOJI.get(level, '')} *[{env_tag}] {title}*"
    if message:
        text += f"\n{message}"
    try:
        resp = requests.post(url, json={"text": text}, timeout=_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("Slack 발송 실패 HTTP %s (제목=%s)", resp.status_code, title)
            return False
        return True
    except requests.RequestException as exc:
        # URL 마스킹: 예외 문자열에 웹훅 경로가 섞일 수 있어 타입만 남긴다.
        logger.warning("Slack 발송 예외 %s (제목=%s)", type(exc).__name__, title)
        return False


def notify_info(title: str, message: str = "") -> bool:
    return notify("info", title, message)


def notify_warn(title: str, message: str = "") -> bool:
    return notify("warn", title, message)


def notify_error(title: str, message: str = "") -> bool:
    return notify("error", title, message)
