"""라우터 통합.

각 라우트 모듈(health, market, predictions, ...)을 하나의 api_router 로 묶어
api.main 에서 한 번에 등록한다. 새 라우터는 여기에 include_router 로 추가한다.

인증: /health 를 제외한 전 라우트에 X-API-Key 검증(require_api_key)을 건다.
API_AUTH_KEY 미설정 시 검증은 no-op(로컬 개발) — 외부 배포에서는 반드시 설정(§2.6).
"""

from fastapi import APIRouter, Depends

from api.dependencies import require_api_key
from api.routers import decision, flow, health, market, ml, pipeline, recommendation, trade

_AUTH = [Depends(require_api_key)]

api_router = APIRouter()
api_router.include_router(health.router)  # 헬스체크만 무인증(LB/모니터링용)
api_router.include_router(pipeline.router, dependencies=_AUTH)
api_router.include_router(market.router, dependencies=_AUTH)
api_router.include_router(flow.router, dependencies=_AUTH)
api_router.include_router(ml.router, dependencies=_AUTH)
api_router.include_router(recommendation.router, dependencies=_AUTH)
api_router.include_router(decision.router, dependencies=_AUTH)
api_router.include_router(trade.router, dependencies=_AUTH)
