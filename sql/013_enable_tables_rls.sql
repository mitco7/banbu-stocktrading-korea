-- 전 테이블 RLS 활성화. service_role 은 RLS 를 우회하므로 서버(서비스키)는 영향 없음.
-- 멱등: 정책이 이미 있어도 DROP IF EXISTS 후 재생성(재실행 시 42710 방지).
ALTER TABLE market_data            ENABLE ROW LEVEL SECURITY;
ALTER TABLE investor_flow          ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_analysis_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE predicted_stocks       ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_recommendations  ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_logs          ENABLE ROW LEVEL SECURITY;
ALTER TABLE trade_records          ENABLE ROW LEVEL SECURITY;
ALTER TABLE access_tokens          ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_runs          ENABLE ROW LEVEL SECURITY;

-- Kaggle(anon) 전용 최소 권한: 학습 데이터 읽기 + 결과 2테이블 갱신.
DROP POLICY IF EXISTS kaggle_read_market ON market_data;
CREATE POLICY kaggle_read_market ON market_data
    FOR SELECT TO anon USING (true);

DROP POLICY IF EXISTS kaggle_write_pred ON predicted_stocks;
CREATE POLICY kaggle_write_pred ON predicted_stocks
    FOR INSERT TO anon WITH CHECK (true);
DROP POLICY IF EXISTS kaggle_clear_pred ON predicted_stocks;
CREATE POLICY kaggle_clear_pred ON predicted_stocks
    FOR DELETE TO anon USING (true);

DROP POLICY IF EXISTS kaggle_write_analysis ON stock_analysis_results;
CREATE POLICY kaggle_write_analysis ON stock_analysis_results
    FOR INSERT TO anon WITH CHECK (true);
DROP POLICY IF EXISTS kaggle_clear_analysis ON stock_analysis_results;
CREATE POLICY kaggle_clear_analysis ON stock_analysis_results
    FOR DELETE TO anon USING (true);

-- 나머지 테이블은 anon 정책 없음 = 접근 전면 차단(access_tokens·trade_records 등).

-- ⚠ 위 INSERT/DELETE 정책만으로는 Kaggle 저장이 실패한다(42501).
--    supabase-py 가 RETURNING 을 쓰므로 SELECT 정책도 필요 — sql/014 를 반드시 함께 적용할 것.
-- ⚠ SUPABASE_KEY_KOREA 는 service_role 키를 쓸 것. anon/키 오용 시 market_data INSERT 가
--    RLS에 막힌다(백필 42501). service_role 은 RLS 우회.
