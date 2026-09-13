"""구조화 로깅 헬퍼(최소 골격).

지금은 stdout 으로 일관된 포맷의 로그를 내보내는 표준 logging 래퍼다.
이후 Step 8(관측성)에서 JSON 포맷 + 파일 핸들러로 확장한다.
민감정보(토큰/계좌번호) 마스킹은 utils/crypto.py 가 담당한다(CLAUDE.md §2.6).
"""

from __future__ import annotations

import logging
import sys

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_configured = False

# 우리 앱 최상위 패키지(이들만 INFO 로 노출).
_APP_LOGGERS = ("backfill", "services", "utils", "db", "api", "core", "scripts")


def ensure_utf8_streams() -> None:
    """stdout/stderr 를 UTF-8 로 재설정한다.

    Windows 기본 콘솔(cp949)에서는 한글/기호(⚠, ← 등) 출력 시 UnicodeEncodeError 로
    print 가 죽고, 로깅은 해당 문자를 잃는다. UTF-8 로 고정해 이를 방지한다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001 — 재설정 불가 스트림이면 무시
                pass


def _configure_root() -> None:
    """루트 핸들러를 설정하되, 루트 레벨은 WARNING 으로 둔다.

    pykrx 같은 서드파티가 루트 로거로 직접 INFO 로깅을 하며 잘못된 포맷
    (logging.info(args, kwargs))을 호출해 'Logging error' 트레이스백을 유발한다.
    루트 레벨을 WARNING 으로 올려 이를 차단하고, 우리 앱 패키지 로거만 INFO 로 노출한다
    (핸들러는 공유하므로 우리 INFO 로그는 그대로 출력됨).
    """
    global _configured
    if _configured:
        return
    ensure_utf8_streams()  # 핸들러 생성 전에 스트림을 UTF-8 로 고정.
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    for pkg in _APP_LOGGERS:
        logging.getLogger(pkg).setLevel(logging.INFO)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """모듈별 로거를 반환한다.

    사용:
        from utils.logger import get_logger
        logger = get_logger(__name__)
    """
    _configure_root()
    return logging.getLogger(name)
