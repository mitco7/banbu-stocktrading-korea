"""FastAPI 앱 진입점.

헬스체크/조회용 API 를 제공한다. 매매 파이프라인 자체는 스케줄러(services/scheduler)가
구동하며, 이 앱은 그 상태를 관측·조회하는 창구다(ARCHITECTURE.md §1).

lifespan 에서 시작/종료 로그를 남기고, 추후 스케줄러 기동·초기 수집 연결 지점을 마련해 둔다.
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from api.routers import api_router
from core.config import settings
from services import scheduler
from utils.logger import get_logger

logger = get_logger(__name__)


async def _startup_incremental_collect() -> None:
    """기동 시 증분 수집(마지막 저장일 이후 ~ 어제). 블로킹이라 스레드로, 실패해도 앱은 계속."""
    try:
        from services.market_data_service import update_market_data

        result = await asyncio.to_thread(update_market_data)
        logger.info("기동 시 증분 수집 결과: %s", result)
    except Exception as exc:  # noqa: BLE001 — 수집 실패가 앱 기동을 막지 않게
        logger.warning("기동 시 증분 수집 실패(앱은 계속 기동): %s", exc)


def _supabase_key_role() -> str:
    """Supabase 키의 role 클레임만 추출(값 미노출). 판별 불가 시 'unknown'.

    RLS(sql/013) 적용 후 서버가 anon 키로 돌면 대부분 테이블이 **에러 없이 0행**으로 보여
    (보유 종목 미인식 → 중복 매수 등) 조용한 사고가 난다. 기동 로그에서 즉시 식별하기 위함.
    """
    key = settings.SUPABASE_KEY_KOREA
    parts = key.split(".")
    if len(parts) != 3:
        return "non-jwt"
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role", "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def _log_credential_presence() -> None:
    """핵심 자격증명의 '설정 여부만' 로그(값 출력 금지 §2.6).

    .env 를 못 찾은 채 '정상 기동'하는 사고(systemd WorkingDirectory 누락 등)를
    기동 로그 한 줄로 즉시 식별하기 위함.
    """
    checks = {
        "SUPABASE": bool(settings.SUPABASE_URL_KOREA and settings.SUPABASE_KEY_KOREA),
        "KIS": bool(settings.kis_appkey and settings.kis_appsecret and settings.kis_cano),
        "ANTHROPIC": bool(settings.ANTHROPIC_API_KEY),
        "KAGGLE": bool(settings.KAGGLE_API_TOKEN or (settings.KAGGLE_USERNAME and settings.KAGGLE_KEY)),
        "SLACK": bool(settings.SLACK_WEBHOOK_URL),
        "API_AUTH": bool(settings.API_AUTH_KEY),
    }
    logger.info("자격증명 설정 여부: %s",
                ", ".join(f"{k}={'OK' if v else '없음'}" for k, v in checks.items()))
    missing = [k for k, v in checks.items() if not v and k not in ("SLACK", "API_AUTH")]
    if missing:
        logger.warning("필수 자격증명 누락: %s — .env 경로/내용 확인 필요", ", ".join(missing))
    if not settings.API_AUTH_KEY:
        logger.warning("API_AUTH_KEY 미설정 — API 인증 비활성(로컬 전용). 외부 배포 시 반드시 설정(§2.6).")

    # RLS 적용 환경에서 서버 키가 service_role 이 아니면 조회가 '0행'으로 조용히 실패한다.
    role = _supabase_key_role()
    logger.info("Supabase 키 role=%s, Kaggle 주입키 분리=%s",
                role, "OK" if settings.SUPABASE_KAGGLE_KEY else "없음")
    if role != "service_role":
        logger.warning(
            "Supabase 키가 service_role 이 아닙니다(role=%s). RLS 활성 상태라면 보유/결정 테이블이 "
            "빈 결과로 보여 중복 매수 등 사고가 날 수 있습니다 — .env 확인 필요.", role,
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ── 시작 ─────────────────────────────────────────────
    logger.info(
        "앱 시작 — KIS 모드=%s, scoring_v2=%s",
        settings.kis_environment,
        settings.USE_SCORING_V2,
    )
    _log_credential_presence()
    # 기동 시 증분 수집(백그라운드 — 기동을 막지 않음). 개발 reload 시엔 COLLECT_ON_STARTUP=false 권장.
    if settings.COLLECT_ON_STARTUP:
        asyncio.create_task(_startup_incremental_collect())
    # 스케줄러 기동(파이프라인 06:00·매수 10:00·매도 감시 1분·마감 reconcile — services/scheduler).
    # 시각 설정 오류 등은 여기서 즉시 실패시켜 '스케줄 없이 조용히 기동'하는 사고를 막는다.
    if settings.SCHEDULER_ENABLED:
        scheduler.start()
    else:
        logger.warning("SCHEDULER_ENABLED=false — 자동 매매 스케줄 비활성(수동 API 호출만 동작)")
    yield
    # ── 종료 ─────────────────────────────────────────────
    scheduler.shutdown()
    logger.info("앱 종료")


app = FastAPI(
    title="banbu-stocktrading-korea",
    description="한국 주식(KOSPI/KOSDAQ) 자동매매 백엔드 — 헬스체크/조회 API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router)


@app.get("/", tags=["root"])
def root() -> dict[str, str]:
    return {
        "service": "banbu-stocktrading-korea",
        "message": "한국 주식 자동매매 백엔드. 상태 확인은 GET /health, API 문서는 /docs 참고.",
        "docs": "/docs",
        "health": "/health",
    }
