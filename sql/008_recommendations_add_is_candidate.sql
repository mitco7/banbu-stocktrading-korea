-- 기존 stock_recommendations 테이블에 is_candidate 컬럼 추가(이미 만든 테이블용 마이그레이션).
-- 신규 설치는 007 에 포함되어 있으므로 이 파일은 실행 불필요.
-- is_candidate = 매수 후보 여부(predicted_rise_pct >= rise_threshold). 후보가 아니어도 25종목 전부 저장됨.

ALTER TABLE stock_recommendations ADD COLUMN IF NOT EXISTS is_candidate BOOLEAN;

-- (선택) 기존에 저장된 행이 있으면 후보 여부를 소급 계산해 채운다.
UPDATE stock_recommendations
   SET is_candidate = (predicted_rise_pct >= rise_threshold)
 WHERE is_candidate IS NULL
   AND predicted_rise_pct IS NOT NULL
   AND rise_threshold IS NOT NULL;

-- 후보 빠른 조회용 인덱스(선택).
CREATE INDEX IF NOT EXISTS idx_stock_recommendations_candidate
    ON stock_recommendations ("날짜", is_candidate);
