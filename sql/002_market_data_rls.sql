-- market_data RLS 비활성화.
-- ⚠ 중요(사고 예방): Supabase 는 신규 테이블에 RLS 가 켜질 수 있고, 정책이 없으면
--    service/anon 키로 SELECT 시 '데이터가 있어도 0행'이 반환된다.
--    그러면 백필이 '데이터 없음'으로 오판해 과거부터 재수집하는 사고가 난다.
--    이 테이블은 서버(서비스 키)만 접근하는 내부 데이터이므로 RLS 를 끈다.
ALTER TABLE market_data DISABLE ROW LEVEL SECURITY;
