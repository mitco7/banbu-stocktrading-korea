"""ML(Kaggle) 트리거 라우트 (FastAPI 컨트롤러).

컨트롤러는 얇게 유지하고 로직은 services/ml_trigger_service 에 위임한다.
Kaggle CLI(kaggle) 설치 + KAGGLE_* 자격증명이 필요하다.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from services import ml_trigger_service
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/ml", tags=["ml"])


@router.get("/auth-check")
def auth_check() -> dict:
    """Kaggle 인증 확인(kernels list 호출). 완료 기준: 로컬에서 auth-check 가능."""
    ok, msg = ml_trigger_service.check_auth()
    return {"ok": ok, "message": msg}


@router.post("/trigger")
def trigger() -> dict:
    """Kaggle 노트북 push(= 학습 실행 트리거). 비동기 — 상태는 /ml/status 로 폴링.

    push 직전 predict.py + .env SUPABASE 시크릿으로 predict.ipynb 를 재생성해 전송한다.
    """
    try:
        ok, msg = ml_trigger_service.push_kernel()
    except RuntimeError as exc:  # 자격증명 누락 등
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=502, detail=msg)
    return {"ok": True, "message": msg}


@router.get("/status")
def status() -> dict:
    """현재 Kaggle 커널 실행 상태(queued/running/complete/error/unknown)."""
    try:
        return {"status": ml_trigger_service.get_status()}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
