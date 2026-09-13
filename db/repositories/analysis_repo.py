"""stock_analysis_results 조회 (ML 예측 결과 — 읽기 전용).

predict.py(Kaggle)가 delete 후 insert 로 매 실행 전체 갱신하므로, 테이블에는 항상
'가장 최근 1회 분석'만 존재한다. 본 레포는 그 결과를 읽기만 한다(쓰기는 ML 파이프라인 담당).

⚠ 컬럼 "Rise Probability (%)" 는 이름과 달리 **확률이 아니라 예측 상승률(%)**
   = (예측가-현재가)/현재가×100 이다(predict.py analyze_rise_predictions). CLAUDE.md §2.2 에 따라
   본 코드/저장 컬럼에서는 'predicted_rise_pct'(예측 상승률)로 다룬다.
"""

from __future__ import annotations

from db.client import get_supabase
from utils.logger import get_logger

logger = get_logger(__name__)

TABLE = "stock_analysis_results"


def fetch_latest_analysis() -> list[dict]:
    """현재 저장된 ML 분석 결과 전체(종목별 1행)를 반환. 없으면 빈 리스트."""
    client = get_supabase()
    resp = client.table(TABLE).select("*").execute()
    return resp.data or []
