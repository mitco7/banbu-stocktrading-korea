"""최종 의사결정 라우트 (FastAPI 컨트롤러) — 종합점수 + VIX 게이트 + LLM 거부권.

컨트롤러는 얇게 유지하고 로직은 services/scoring_service 에 위임한다.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from services import scoring_service
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.post("/run")
def run(date: str | None = None) -> dict:
    """매수 후보 점수화 → VIX 게이트 → LLM 거부권 검토 → decision_logs 저장.

    date: 결정 산출일 YYYY-MM-DD(기본 오늘 KST).
    """
    try:
        return scoring_service.score_and_decide(date)
    except RuntimeError as exc:  # Supabase 자격증명 누락 등
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("")
def list_decisions(date: str | None = None) -> dict:
    """특정 날짜(기본 최근)의 결정 로그 조회."""
    try:
        return scoring_service.list_for_date(date)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
