"""매수 후보 라우트 (FastAPI 컨트롤러).

컨트롤러는 얇게 유지하고 로직은 services/recommendation_service 에 위임한다.
ML 예측(stock_analysis_results) + 기술적 지표 + 수급(investor_flow) → stock_recommendations.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from services import recommendation_service
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


@router.post("/generate")
def generate(ref_date: str | None = None, min_rise_pct: float | None = None) -> dict:
    """ML 1차 후보(예측 상승률 ≥ 임계값)에 기술적 지표 + 수급 결합 후 저장.

    ref_date: 추천 산출일 YYYY-MM-DD(기본 오늘 KST). min_rise_pct: 1차 필터 임계값(%, 기본 3).
    """
    try:
        return recommendation_service.generate_recommendations(ref_date, min_rise_pct)
    except RuntimeError as exc:  # Supabase 자격증명 누락 등
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("")
def list_recommendations(date: str | None = None) -> dict:
    """특정 날짜(기본 최근일)의 매수 후보 목록 조회."""
    try:
        return recommendation_service.list_for_date(date)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
