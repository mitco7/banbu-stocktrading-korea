"""외국인·기관 수급 점수 라우트 (FastAPI 컨트롤러).

컨트롤러는 얇게 유지하고 비즈니스 로직은 services/flow_service 에 위임한다.
⚠ 수급 데이터 소스는 pykrx(KRX)다. KRX 접속이 막힌 환경에서는 0건 반환된다.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from services import flow_service
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/flow", tags=["flow"])


@router.post("/collect")
def collect(lookback_days: int | None = None, ref_date: str | None = None) -> dict:
    """25종목의 외국인·기관 순매수 → flow_score(-1~+1) 산출 후 investor_flow 에 스냅샷 저장.

    lookback_days: 합산 거래일 수(기본 설정값 5). ref_date: 기준일 YYYY-MM-DD(기본 어제).
    """
    try:
        return flow_service.collect_and_store_flows(lookback_days, ref_date)
    except RuntimeError as exc:  # Supabase 자격증명 누락 등
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/scores")
def scores() -> dict:
    """가장 최근 기준일의 수급 점수 스냅샷 조회."""
    from db.repositories import flow_repo

    try:
        rows = flow_repo.fetch_latest_snapshot()
        calc_date = rows[0]["calculation_date"] if rows else None
        return {"calculation_date": calc_date, "count": len(rows), "flows": rows}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
