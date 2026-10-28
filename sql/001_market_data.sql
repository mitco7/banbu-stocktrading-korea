-- market_data: 시장데이터 와이드 테이블 (KRX 영업일당 1행).
-- 자동 생성: scripts/gen_schema_sql.py (직접 편집 금지).
-- 유니버스 기준일(SELECTED_AT): 2026-05-31
-- 날짜 축: KRX 영업일만(주말·휴장일 행 없음). 휴장 목록=core/market_calendar.KRX_HOLIDAYS.
--          결측은 영업일 그리드 위 ffill만(CLAUDE.md §2.1). 거래량 결측은 NULL.
-- 컬럼 단위: 모두 FLOAT8. 결측은 NULL(상장 전 구간·미수집 지표 등).
-- 컬럼명=한글(ML 의 TARGET_COLUMNS/ECONOMIC_FEATURES 와 직접 매칭).
-- ⚠ 해외 시장 컬럼은 t-1 lag, 거시는 발표일 시프트가 ML 피처 단계에서 필요(core/market_spec.py).
-- ⚠ 스키마 변경 후 재적용 시: 아래 CREATE 는 IF NOT EXISTS 라 '기존 테이블을 갱신하지 않는다'.
--    컬럼이 추가/변경됐다면 먼저 기존 테이블을 비우고 드롭해야 한다(데이터 있으면 백업 후):
--      DROP TABLE IF EXISTS market_data CASCADE;

CREATE TABLE IF NOT EXISTS market_data (
    "날짜" DATE PRIMARY KEY
    -- ── 거시/시장 지표 ──
    , "KOSPI" FLOAT8  -- [kr_index/D] 코스피(당일 장마감). 25종목 구성원 — 미래 시프트 금지.
    , "KOSDAQ" FLOAT8  -- [kr_index/D] 코스닥. 위험선호 프록시.
    , "원/달러 환율" FLOAT8  -- [fx/D] 외국인 수급 직결. 한국 장마감 스냅샷 권장.
    , "달러 인덱스" FLOAT8  -- [fx/D] 달러강세=외국인 순매도 프록시. FDR 'DX'(선물연속)는 값오류 → FDR 'DX-Y.NYB'(crumb-free) 우선, yfinance 폴백.
    , "미국 달러지수(무역가중)" FLOAT8  -- [fx/D] FRED 무역가중 달러(브로드). 달러인덱스와 보완.
    , "엔/원 환율" FLOAT8  -- [fx/D] 엔 약세=한국 수출(자동차·반도체) 경쟁력 프록시.
    , "달러/엔" FLOAT8  -- [fx/D] 엔 캐리·아시아 환율. FDR 우선(yfinance 429 회피).
    , "달러/위안" FLOAT8  -- [fx/D] 위안 환율(중국 익스포저). FDR 우선.
    , "WTI 유가" FLOAT8  -- [commodity/D] 정유·화학·항공/조선 비용.
    , "금 가격" FLOAT8  -- [commodity/D] 안전자산·달러 역상관. FDR 우선(yfinance 429 회피).
    , "필라델피아 반도체지수" FLOAT8  -- [global_index/D] 삼성·하이닉스 핵심 선행. t-1 lag 필수. FDR(crumb-free) 우선, yfinance 폴백.
    , "나스닥 종합지수" FLOAT8  -- [global_index/D] 성장주 위험선호. t-1 lag.
    , "나스닥 100" FLOAT8  -- [global_index/D] 빅테크 집중. FDR 우선(yfinance 429 회피).
    , "S&P 500 지수" FLOAT8  -- [global_index/D] 미국 대형주 종합.
    , "다우존스 지수" FLOAT8  -- [global_index/D] 미국 산업주.
    , "VIX 지수" FLOAT8  -- [global_index/D] 미국 변동성(공포). VKOSPI 대용. t-1 lag.
    , "닛케이 225" FLOAT8  -- [global_index/D] 일본 증시(아시아 동조·엔). FDR 우선(yfinance 429 회피).
    , "상해종합" FLOAT8  -- [global_index/D] 중국 본토 증시(수출·소재 동조). FDR 우선.
    , "항셍" FLOAT8  -- [global_index/D] 홍콩 증시(중국 익스포저). FDR 우선.
    , "영국 FTSE" FLOAT8  -- [global_index/D] 유럽 위험선호. FDR(crumb-free) 우선, yfinance 폴백.
    , "독일 DAX" FLOAT8  -- [global_index/D] 유럽 제조·수출. FDR(crumb-free) 우선, yfinance 폴백.
    , "프랑스 CAC 40" FLOAT8  -- [global_index/D] 유럽 증시. FDR(crumb-free) 우선, yfinance 폴백.
    , "미국 채권 ETF" FLOAT8  -- [etf/D] AGG(미국 종합채권). 금리·위험회피.
    , "물가연동채 ETF" FLOAT8  -- [etf/D] TIP(TIPS). 실질금리·기대인플레.
    , "투자등급 회사채 ETF" FLOAT8  -- [etf/D] LQD. 신용 스프레드.
    , "미국 리츠 ETF" FLOAT8  -- [etf/D] VNQ. 부동산·금리 민감.
    , "러셀 2000 ETF" FLOAT8  -- [etf/D] IWM(소형주). 위험선호 강도.
    , "미국 기준금리" FLOAT8  -- [us_macro/D] FRED DFF(일간 실효 연방기금금리).
    , "미국 10년물 국채금리" FLOAT8  -- [us_macro/D] FRED DGS10 우선(yfinance 429 회피). 성장주 밸류·외국인 수급.
    , "미국 2년물 국채금리" FLOAT8  -- [us_macro/D] FRED DGS2. 정책금리 기대.
    , "장단기 금리차" FLOAT8  -- [us_macro/D] FRED T10Y2Y(10년-2년). 경기/침체 시그널.
    , "기대 인플레이션" FLOAT8  -- [us_macro/D] FRED T10YIE(10년 기대인플레).
    , "미국 소비자물가지수" FLOAT8  -- [us_macro/M] FRED CPIAUCSL(월). 발표일 +1개월 시프트.
    , "미국 실업률" FLOAT8  -- [us_macro/M] FRED UNRATE(월).
    , "미시간 소비자심리지수" FLOAT8  -- [us_macro/M] FRED UMCSENT(월).
    , "미국 M2 통화량" FLOAT8  -- [us_macro/M] FRED M2SL(월). 유동성.
    , "미국 개인소비지출" FLOAT8  -- [us_macro/M] FRED PCE(월).
    , "미국 금융스트레스지수" FLOAT8  -- [us_macro/W] FRED STLFSI4(주). +1주 시프트.
    , "미국 모기지금리" FLOAT8  -- [us_macro/W] FRED MORTGAGE30US(주, 30년 고정).
    , "미국 GDP" FLOAT8  -- [us_macro/Q] FRED GDPC1(분기 실질). +1분기 시프트.
    , "미국 가계부채비율" FLOAT8  -- [us_macro/Q] FRED TDSP(분기).
    , "한국 기준금리" FLOAT8  -- [kr_macro/IRR] ECOS 722Y001/0101000(검증: 2024-12=3.0). 변경일까지 ffill.
    , "한국 소비자물가지수" FLOAT8  -- [kr_macro/M] ECOS 901Y009/0=총지수(검증 ~114). 발표일 +1개월 시프트.
    , "수출금액지수" FLOAT8  -- [kr_macro/M] ECOS 403Y001/*AA=총지수. 반도체 수출 사이클.
    , "경기선행지수" FLOAT8  -- [kr_macro/M] ECOS 901Y067/I16E 순환변동치. ⚠ revision(vintage 미확보 시 ML 제외 권고).
    , "산업생산지수" FLOAT8  -- [kr_macro/M] ECOS 901Y033/A00+item2=2(계절조정). ⚠ revision.
    -- ── 25종목 종가(수정주가) ──
    , "삼성전자" FLOAT8  -- 005930
    , "SK하이닉스" FLOAT8  -- 000660
    , "LG에너지솔루션" FLOAT8  -- 373220
    , "삼성바이오로직스" FLOAT8  -- 207940
    , "현대차" FLOAT8  -- 005380
    , "기아" FLOAT8  -- 000270
    , "현대모비스" FLOAT8  -- 012330
    , "삼성물산" FLOAT8  -- 028260
    , "NAVER" FLOAT8  -- 035420
    , "포스코홀딩스" FLOAT8  -- 005490
    , "LG화학" FLOAT8  -- 051910
    , "셀트리온" FLOAT8  -- 068270
    , "한화에어로스페이스" FLOAT8  -- 012450
    , "HD현대중공업" FLOAT8  -- 329180
    , "두산에너빌리티" FLOAT8  -- 034020
    , "삼성전기" FLOAT8  -- 009150
    , "카카오" FLOAT8  -- 035720
    , "크래프톤" FLOAT8  -- 259960
    , "하이브" FLOAT8  -- 352820
    , "SK텔레콤" FLOAT8  -- 017670
    , "에코프로머티" FLOAT8  -- 450080
    , "한국항공우주" FLOAT8  -- 047810
    , "LG전자" FLOAT8  -- 066570
    , "SK이노베이션" FLOAT8  -- 096770
    , "HL만도" FLOAT8  -- 204320
    -- ── 25종목 거래량 ──
    , "삼성전자_거래량" FLOAT8  -- 005930
    , "SK하이닉스_거래량" FLOAT8  -- 000660
    , "LG에너지솔루션_거래량" FLOAT8  -- 373220
    , "삼성바이오로직스_거래량" FLOAT8  -- 207940
    , "현대차_거래량" FLOAT8  -- 005380
    , "기아_거래량" FLOAT8  -- 000270
    , "현대모비스_거래량" FLOAT8  -- 012330
    , "삼성물산_거래량" FLOAT8  -- 028260
    , "NAVER_거래량" FLOAT8  -- 035420
    , "포스코홀딩스_거래량" FLOAT8  -- 005490
    , "LG화학_거래량" FLOAT8  -- 051910
    , "셀트리온_거래량" FLOAT8  -- 068270
    , "한화에어로스페이스_거래량" FLOAT8  -- 012450
    , "HD현대중공업_거래량" FLOAT8  -- 329180
    , "두산에너빌리티_거래량" FLOAT8  -- 034020
    , "삼성전기_거래량" FLOAT8  -- 009150
    , "카카오_거래량" FLOAT8  -- 035720
    , "크래프톤_거래량" FLOAT8  -- 259960
    , "하이브_거래량" FLOAT8  -- 352820
    , "SK텔레콤_거래량" FLOAT8  -- 017670
    , "에코프로머티_거래량" FLOAT8  -- 450080
    , "한국항공우주_거래량" FLOAT8  -- 047810
    , "LG전자_거래량" FLOAT8  -- 066570
    , "SK이노베이션_거래량" FLOAT8  -- 096770
    , "HL만도_거래량" FLOAT8  -- 204320
    -- ── 감사 컬럼 ──
    , created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    , updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- updated_at 자동 갱신 트리거.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_market_data_updated_at ON market_data;
CREATE TRIGGER trg_market_data_updated_at BEFORE UPDATE ON market_data
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

