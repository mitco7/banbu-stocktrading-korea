"""테이블별 CRUD(repository) 레이어.

ARCHITECTURE.md 레이어 의존 규칙: **db/client(Supabase)에 접근하는 것은 오직 이 패키지뿐**이다.
services/ 등 상위 레이어는 get_supabase() 를 직접 호출하지 말고 여기 정의된 repository 함수만 사용한다.
실제 repository 모듈(market_repo, trade_repo 등)은 Step 2 이후 추가된다.
"""
