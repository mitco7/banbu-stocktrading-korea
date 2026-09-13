"""외부 API 호출용 재시도 데코레이터 (CLAUDE.md §외부 API: 재시도+타임아웃+로깅).

⚠ 주의: 멱등하지 않은 작업(특히 주문 송신)에는 절대 사용하지 말 것.
        시세/시장데이터 수집처럼 안전하게 반복 가능한 읽기 호출에만 사용한다(CLAUDE.md §2.7).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import wraps
from typing import TypeVar

from utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


def retry(
    attempts: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    non_retryable: tuple[type[Exception], ...] = (ImportError,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """지수 백오프 재시도. 읽기 전용(멱등) 외부 호출에만 사용.

    Args:
        attempts: 총 시도 횟수(최초 1회 포함).
        base_delay: 첫 재시도 전 대기(초).
        backoff: 재시도마다 대기 배수.
        exceptions: 재시도 대상 예외.
        non_retryable: 재시도해도 안 풀리는 예외(즉시 재발생, 대기 없음).
            기본 ImportError/ModuleNotFoundError — 모듈 미설치는 재시도 무의미.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            delay = base_delay
            last_exc: Exception | None = None
            for i in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except non_retryable as exc:
                    logger.warning("%s 실패(재시도 안 함): %s", func.__name__, exc)
                    raise
                except exceptions as exc:  # noqa: PERF203
                    last_exc = exc
                    if i < attempts:
                        logger.warning(
                            "%s 실패(%d/%d): %s — %.1fs 후 재시도",
                            func.__name__, i, attempts, exc, delay,
                        )
                        time.sleep(delay)
                        delay *= backoff
                    else:
                        logger.error(
                            "%s 최종 실패(%d/%d): %s", func.__name__, i, attempts, exc
                        )
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator
