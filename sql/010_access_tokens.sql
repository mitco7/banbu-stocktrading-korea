-- access_tokens: KIS OpenAPI 접근토큰 캐시 (Step 6).
-- KIS 토큰은 발급이 1분당 1회로 제한되고 ~24h 유효 → 프로세스 재기동에도 재사용하도록 DB 캐시.
-- 환경(PAPER/PROD)별 1행만 유지(env PK). 만료 임박 시 auth_service 가 갱신·upsert.
-- ⚠ 토큰은 시크릿 → 로그/응답에 노출 금지(utils.crypto 마스킹). RLS 비활성(서비스키 전용).

CREATE TABLE IF NOT EXISTS access_tokens (
    env TEXT PRIMARY KEY,                -- 'PAPER' / 'PROD'
    access_token TEXT NOT NULL,          -- KIS Bearer 토큰
    token_type TEXT DEFAULT 'Bearer',
    expires_at TIMESTAMPTZ NOT NULL,     -- 만료 시각(KST 기준 tz-aware 저장)
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_access_tokens_updated_at ON access_tokens;
CREATE TRIGGER trg_access_tokens_updated_at
    BEFORE UPDATE ON access_tokens
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE access_tokens DISABLE ROW LEVEL SECURITY;
