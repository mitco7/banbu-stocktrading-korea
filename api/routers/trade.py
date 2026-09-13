"""KIS 매매 라우트 (FastAPI 컨트롤러). 모의투자 우선.

컨트롤러는 얇게 유지하고 로직은 services/trading_service 에 위임한다.
⚠ 실거래를 일으키는 엔드포인트 — KIS_USE_MOCK=true(모의) 전제. 실전 전환은 §2.4 별도 절차.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from services import trading_service
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/trade", tags=["trade"])


@router.post("/buy")
def buy(date: str | None = None) -> dict:
    """decision_logs 의 BUY 종목 매수(기본 최근 결정일). 중복방지·10% 배분·ATR TP/SL 저장."""
    try:
        return trading_service.execute_buys(date)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/sell-cycle")
def sell_cycle() -> dict:
    """보유 종목 매도 점검(장중 1분 주기 호출). TP/SL·기술신호·VIX·time-stop."""
    try:
        return trading_service.run_sell_cycle()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/reconcile")
def reconcile() -> dict:
    """KIS 잔고 ↔ trade_records 정합성 동기화."""
    try:
        return trading_service.reconcile()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/positions")
def positions() -> dict:
    """현재 미청산 포지션 조회."""
    from core.config import settings
    from db.repositories import trade_repo

    try:
        rows = trade_repo.fetch_open_positions(settings.kis_environment)
        return {"env": settings.kis_environment, "count": len(rows), "positions": rows}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
