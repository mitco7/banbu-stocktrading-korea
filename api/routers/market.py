"""시장 데이터 수집/조회 라우트 (FastAPI 컨트롤러).

컨트롤러는 얇게 유지하고 비즈니스 로직은 services/market_data_service 에 위임한다.
수집 트리거는 Supabase 자격증명이 필요하다(미설정 시 503).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services import market_data_service
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/market", tags=["market"])


class CollectRequest(BaseModel):
    start: str = Field(..., description="시작일 YYYY-MM-DD", examples=["2024-01-01"])
    end: str = Field(..., description="종료일 YYYY-MM-DD", examples=["2024-01-31"])


class CollectResponse(BaseModel):
    start: str
    end: str
    upserted: int


@router.post("/collect", response_model=CollectResponse)
def collect(req: CollectRequest) -> CollectResponse:
    """[start, end] 구간을 수집해 market_data 에 멱등 저장.

    ⚠ 동기 호출이라 넓은 구간은 수 분 블로킹된다 — 대량 적재는 scripts/backfill.py 사용 권장.
    """
    try:
        result = market_data_service.collect_and_save(req.start, req.end)
    except RuntimeError as exc:  # Supabase 자격증명 누락 등
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:    # 잘못된 날짜 구간 등
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CollectResponse(start=req.start, end=req.end, **result)


@router.post("/update")
def update(force: bool = False) -> dict:
    """마지막 저장일+1 ~ 어제까지 증분 수집·저장(멱등). 장중엔 당일 제외.

    force=true: 당일 포함 + 과도 범위 안전장치 무시.
    """
    try:
        return market_data_service.update_market_data(force=force)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/status")
def status() -> dict:
    """저장된 market_data 의 행 수와 날짜 범위."""
    from db.repositories import market_data_repo

    try:
        lo, hi = market_data_repo.date_range()
        return {"rows": market_data_repo.count_rows(), "min_date": lo, "max_date": hi}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
