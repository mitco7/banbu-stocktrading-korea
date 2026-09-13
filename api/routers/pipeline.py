"""일일 파이프라인 일괄 실행 라우트 (FastAPI 컨트롤러).

기존 5개 API 를 올바른 의존 순서로 묶은 단일 진입점. 로직은 services/pipeline_service 에 위임.

  1. POST /market/update      — 시장 데이터 증분 수집
  2. POST /flow/collect       — 수급(외국인·기관) 수집
  3. POST /ml/trigger + GET /ml/status 폴링 — ML 예측(완료까지 대기, 최대 15분)
  4. POST /recommendations/generate — 매수 후보 생성
  5. POST /decisions/run      — 종합점수 + VIX 게이트 + LLM 최종결정

⚠ 동기 실행 — 3단계 ML 대기 때문에 응답까지 수 분~최대 PIPELINE_ML_TIMEOUT_SEC(기본 15분)
블로킹된다. 호출 클라이언트의 read timeout 을 충분히 크게 잡을 것.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from services import pipeline_service
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.post("/run")
def run(force_market: bool = False, skip_ml: bool = False) -> dict:
    """Step 1~5 순차 실행 후 BUY/HOLD/후보미달 종목의 단계별 산출물을 상세 반환.

    - force_market: 장중에도 당일 포함 수집.
    - skip_ml: Kaggle 트리거·대기 생략(기존 ML 결과 재사용 — stale 이면 4단계가 중단시킴).
    - 응답: steps(단계별 결과), market_blocked, buy/hold/non_candidates(종목 상세).
      어느 단계든 실패하면 status=failed + failed_step 으로 중단 지점을 알려준다(fail-close).
    """
    try:
        return pipeline_service.run_daily_pipeline(
            force_market=force_market, skip_ml=skip_ml
        )
    except RuntimeError as exc:  # Supabase 자격증명 누락 등
        raise HTTPException(status_code=503, detail=str(exc)) from exc
