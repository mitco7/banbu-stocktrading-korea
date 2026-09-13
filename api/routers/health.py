"""헬스체크 라우트.

GET /health           — 프로세스 생존 여부(외부 의존 없이 항상 200).
GET /health/external  — (이후 Step 8) 외부 서비스 핑 상태. 지금은 자리만 둔다.
"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
