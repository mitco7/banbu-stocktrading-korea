-- sql/013 보완: Kaggle(anon)이 ML 결과 테이블을 저장하려면 SELECT 정책도 필요하다.
--
-- 이유: supabase-py 의 insert()/delete() 는 기본이 returning="representation" 이라
--   실제 실행이 `INSERT ... RETURNING *` / `DELETE ... RETURNING *` 이다.
--   PostgreSQL 은 RETURNING 절에 대해 **SELECT 정책까지** 요구하므로, INSERT/DELETE 정책만
--   있으면 42501(new row violates row-level security policy)로 거부된다.
--   (kaggle_notebook/predict.py 의 save_predictions_to_db / save_analysis_to_db 가 이 경로.)
--
-- 노출 범위: ML 예측 산출물 2개 테이블의 읽기만 추가로 허용한다. 시크릿·포지션이 없는
--   데이터이며, access_tokens·trade_records·decision_logs 등 민감 테이블은 계속 전면 차단.
--
-- ⚠ Supabase SQL Editor 에서 수동 실행(sql/001~013 과 동일 방식).

CREATE POLICY kaggle_read_pred ON predicted_stocks
    FOR SELECT TO anon USING (true);

CREATE POLICY kaggle_read_analysis ON stock_analysis_results
    FOR SELECT TO anon USING (true);
