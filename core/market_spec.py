"""시장 데이터 수집 스펙 (외부화된 설정 — CLAUDE.md §2.3).

market_data 와이드 테이블의 컬럼 = 거시/시장 시계열(MACRO_MARKET_SERIES) + 25종목 종가/거래량.

컬럼 명명 규칙(정답지=미국 시스템과 동일 패턴, ML 드롭인 호환):
  - **컬럼명 = 한글 지표/종목명**. ML 이 `data[TARGET_COLUMNS]`/`data[ECONOMIC_FEATURES]`/`data["날짜"]`
    처럼 한글명으로 직접 인덱싱하므로 DB 컬럼명도 한글로 맞춘다.
  - 종목 종가 컬럼 = 한글 종목명("삼성전자" ...) = core.universe.TARGET_COLUMNS.
  - 종목 거래량 컬럼 = "<종목명>_거래량". 날짜 컬럼 = "날짜".

지표 범위(2026-05 정답지 검토 후 확장): 정답지가 37개 거시/시장 피처로 실제 작동하므로,
한국 시장 특성(글로벌 동조성 큼)을 반영해 동등 breadth(~48)로 확장한다.
  - KR 핵심(지수·환율·한국거시) + 미국 거시(FRED) + 글로벌 지수/원자재 + 채권/리츠 ETF.

────────────────────────────────────────────────────────────────────────────
⚠ look-ahead bias 방지 (CLAUDE.md §2.1):
  - 일간 해외 시계열(lag_days=1)은 KST 영업일과 같은 날짜 join 시 look-ahead → ML 피처 빌더에서 shift(1).
  - 저빈도(주/월/분기) 지표는 발표 시차를 보수적으로 한 주기 시프트해 "알 수 있게 된 날짜"로 노출
    (market_data_service._apply_publication_shift). 정답지는 이 보정이 없어 look-ahead 가 있으나 우리는 회피.
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass

from core.universe import CODE_TO_NAME, KR_UNIVERSE

# 날짜 컬럼명(정답지와 동일).
DATE_COL = "날짜"

TIER_REQUIRED = 1
TIER_OPTIONAL = 2

# resample/ffill·발표시차 분류
FREQ_DAILY = "D"          # 일간 — ffill 한도(연속 3일) 적용, 시프트 없음
FREQ_WEEKLY = "W"         # 주간 — 발표일까지 ffill, +1주 보수 시프트
FREQ_MONTHLY = "M"        # 월간 — 발표일까지 ffill, +1개월 보수 시프트
FREQ_QUARTERLY = "Q"      # 분기 — 발표일까지 ffill, +1분기 보수 시프트
FREQ_IRREGULAR = "IRR"    # 비정기(금통위 등) — 변경일까지 ffill, 시프트 없음

# 저빈도(발표시차 시프트 + 무한 ffill) 대상 freq.
SLOW_FREQS = (FREQ_WEEKLY, FREQ_MONTHLY, FREQ_QUARTERLY, FREQ_IRREGULAR)


@dataclass(frozen=True)
class Series:
    key: str                       # market_data 컬럼명(한글)
    desc: str                      # 설명/메모
    category: str
    freq: str                      # FREQ_*
    lag_days: int                  # ML 피처 빌더에서 적용할 시프트(해외 일간=1, 국내 당일=0)
    tier: int
    sources: tuple = ()            # 폴백 순서: ("provider", spec)


# ──────────────────────────────────────────────────────────────────────────
#  거시/시장 시계열 (~48). key=한글 컬럼명.
# ──────────────────────────────────────────────────────────────────────────
MACRO_MARKET_SERIES: list[Series] = [
    # ── 한국 지수 / 변동성 ────────────────────────────────
    Series("KOSPI", "코스피(당일 장마감). 25종목 구성원 — 미래 시프트 금지.",
           "kr_index", FREQ_DAILY, 0, TIER_REQUIRED, (("fdr", "KS11"), ("yfinance", "^KS11"))),
    Series("KOSDAQ", "코스닥. 위험선호 프록시.",
           "kr_index", FREQ_DAILY, 0, TIER_REQUIRED, (("fdr", "KQ11"), ("yfinance", "^KQ11"))),
    # VKOSPI 제거(2026-05): FDR/yfinance 미제공·pykrx(KRX) 차단으로 수집 불가 → 컬럼에서 제외.
    #   보수 모드(CLAUDE.md §2.4)의 변동성 게이지는 VIX 로 대용한다(stock-quant 권고).

    # ── 환율 ──────────────────────────────────────────────
    Series("원/달러 환율", "외국인 수급 직결. 한국 장마감 스냅샷 권장.",
           "fx", FREQ_DAILY, 0, TIER_REQUIRED, (("fdr", "USD/KRW"), ("yfinance", "KRW=X"))),
    Series("달러 인덱스", "달러강세=외국인 순매도 프록시. FDR 'DX'(선물연속)는 값오류 → FDR 'DX-Y.NYB'(crumb-free) 우선, yfinance 폴백.",
           "fx", FREQ_DAILY, 1, TIER_REQUIRED, (("fdr", "DX-Y.NYB"), ("yfinance", "DX-Y.NYB"))),
    Series("미국 달러지수(무역가중)", "FRED 무역가중 달러(브로드). 달러인덱스와 보완.",
           "fx", FREQ_DAILY, 1, TIER_OPTIONAL, (("fred", {"series_id": "DTWEXBGS"}),)),
    Series("엔/원 환율", "엔 약세=한국 수출(자동차·반도체) 경쟁력 프록시.",
           "fx", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "JPY/KRW"),)),
    Series("달러/엔", "엔 캐리·아시아 환율. FDR 우선(yfinance 429 회피).",
           "fx", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "USD/JPY"), ("yfinance", "JPY=X"))),
    Series("달러/위안", "위안 환율(중국 익스포저). FDR 우선.",
           "fx", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "USD/CNY"), ("yfinance", "CNY=X"))),

    # ── 원자재 ────────────────────────────────────────────
    Series("WTI 유가", "정유·화학·항공/조선 비용.",
           "commodity", FREQ_DAILY, 1, TIER_REQUIRED, (("fdr", "CL=F"), ("yfinance", "CL=F"))),
    Series("금 가격", "안전자산·달러 역상관. FDR 우선(yfinance 429 회피).",
           "commodity", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "GC=F"), ("yfinance", "GC=F"))),

    # ── 글로벌 지수 ───────────────────────────────────────
    Series("필라델피아 반도체지수", "삼성·하이닉스 핵심 선행. t-1 lag 필수. FDR(crumb-free) 우선, yfinance 폴백.",
           "global_index", FREQ_DAILY, 1, TIER_REQUIRED, (("fdr", "^SOX"), ("yfinance", "^SOX"))),
    Series("나스닥 종합지수", "성장주 위험선호. t-1 lag.",
           "global_index", FREQ_DAILY, 1, TIER_REQUIRED, (("fdr", "IXIC"), ("yfinance", "^IXIC"))),
    Series("나스닥 100", "빅테크 집중. FDR 우선(yfinance 429 회피).",
           "global_index", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "NDX"), ("yfinance", "^NDX"))),
    Series("S&P 500 지수", "미국 대형주 종합.",
           "global_index", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "US500"), ("yfinance", "^GSPC"))),
    Series("다우존스 지수", "미국 산업주.",
           "global_index", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "DJI"), ("yfinance", "^DJI"))),
    Series("VIX 지수", "미국 변동성(공포). VKOSPI 대용. t-1 lag.",
           "global_index", FREQ_DAILY, 1, TIER_REQUIRED, (("fdr", "VIX"), ("yfinance", "^VIX"))),
    Series("닛케이 225", "일본 증시(아시아 동조·엔). FDR 우선(yfinance 429 회피).",
           "global_index", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "N225"), ("yfinance", "^N225"))),
    Series("상해종합", "중국 본토 증시(수출·소재 동조). FDR 우선.",
           "global_index", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "SSEC"), ("yfinance", "000001.SS"))),
    Series("항셍", "홍콩 증시(중국 익스포저). FDR 우선.",
           "global_index", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "HSI"), ("yfinance", "^HSI"))),
    Series("영국 FTSE", "유럽 위험선호. FDR(crumb-free) 우선, yfinance 폴백.",
           "global_index", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "^FTSE"), ("yfinance", "^FTSE"))),
    Series("독일 DAX", "유럽 제조·수출. FDR(crumb-free) 우선, yfinance 폴백.",
           "global_index", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "^GDAXI"), ("yfinance", "^GDAXI"))),
    Series("프랑스 CAC 40", "유럽 증시. FDR(crumb-free) 우선, yfinance 폴백.",
           "global_index", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "^FCHI"), ("yfinance", "^FCHI"))),

    # ── 채권/리츠 ETF ─────────────────────────────────────
    # ETF 는 FDR 우선(yfinance 429 회피), yfinance 폴백.
    Series("미국 채권 ETF", "AGG(미국 종합채권). 금리·위험회피.",
           "etf", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "AGG"), ("yfinance", "AGG"))),
    Series("물가연동채 ETF", "TIP(TIPS). 실질금리·기대인플레.",
           "etf", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "TIP"), ("yfinance", "TIP"))),
    Series("투자등급 회사채 ETF", "LQD. 신용 스프레드.",
           "etf", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "LQD"), ("yfinance", "LQD"))),
    Series("미국 리츠 ETF", "VNQ. 부동산·금리 민감.",
           "etf", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "VNQ"), ("yfinance", "VNQ"))),
    Series("러셀 2000 ETF", "IWM(소형주). 위험선호 강도.",
           "etf", FREQ_DAILY, 1, TIER_OPTIONAL, (("fdr", "IWM"), ("yfinance", "IWM"))),

    # ── 미국 금리(일간) ───────────────────────────────────
    Series("미국 기준금리", "FRED DFF(일간 실효 연방기금금리).",
           "us_macro", FREQ_DAILY, 1, TIER_OPTIONAL, (("fred", {"series_id": "DFF"}),)),
    Series("미국 10년물 국채금리", "FRED DGS10 우선(yfinance 429 회피). 성장주 밸류·외국인 수급.",
           "us_macro", FREQ_DAILY, 1, TIER_REQUIRED, (("fred", {"series_id": "DGS10"}), ("yfinance", "^TNX"))),
    Series("미국 2년물 국채금리", "FRED DGS2. 정책금리 기대.",
           "us_macro", FREQ_DAILY, 1, TIER_OPTIONAL, (("fred", {"series_id": "DGS2"}),)),
    Series("장단기 금리차", "FRED T10Y2Y(10년-2년). 경기/침체 시그널.",
           "us_macro", FREQ_DAILY, 1, TIER_OPTIONAL, (("fred", {"series_id": "T10Y2Y"}),)),
    Series("기대 인플레이션", "FRED T10YIE(10년 기대인플레).",
           "us_macro", FREQ_DAILY, 1, TIER_OPTIONAL, (("fred", {"series_id": "T10YIE"}),)),

    # ── 미국 거시(저빈도, FRED) ───────────────────────────
    Series("미국 소비자물가지수", "FRED CPIAUCSL(월). 발표일 +1개월 시프트.",
           "us_macro", FREQ_MONTHLY, 0, TIER_OPTIONAL, (("fred", {"series_id": "CPIAUCSL"}),)),
    Series("미국 실업률", "FRED UNRATE(월).",
           "us_macro", FREQ_MONTHLY, 0, TIER_OPTIONAL, (("fred", {"series_id": "UNRATE"}),)),
    Series("미시간 소비자심리지수", "FRED UMCSENT(월).",
           "us_macro", FREQ_MONTHLY, 0, TIER_OPTIONAL, (("fred", {"series_id": "UMCSENT"}),)),
    Series("미국 M2 통화량", "FRED M2SL(월). 유동성.",
           "us_macro", FREQ_MONTHLY, 0, TIER_OPTIONAL, (("fred", {"series_id": "M2SL"}),)),
    Series("미국 개인소비지출", "FRED PCE(월).",
           "us_macro", FREQ_MONTHLY, 0, TIER_OPTIONAL, (("fred", {"series_id": "PCE"}),)),
    Series("미국 금융스트레스지수", "FRED STLFSI4(주). +1주 시프트.",
           "us_macro", FREQ_WEEKLY, 0, TIER_OPTIONAL, (("fred", {"series_id": "STLFSI4"}),)),
    Series("미국 모기지금리", "FRED MORTGAGE30US(주, 30년 고정).",
           "us_macro", FREQ_WEEKLY, 0, TIER_OPTIONAL, (("fred", {"series_id": "MORTGAGE30US"}),)),
    Series("미국 GDP", "FRED GDPC1(분기 실질). +1분기 시프트.",
           "us_macro", FREQ_QUARTERLY, 0, TIER_OPTIONAL, (("fred", {"series_id": "GDPC1"}),)),
    Series("미국 가계부채비율", "FRED TDSP(분기).",
           "us_macro", FREQ_QUARTERLY, 0, TIER_OPTIONAL, (("fred", {"series_id": "TDSP"}),)),

    # ── 한국 거시(ECOS) ───────────────────────────────────
    Series("한국 기준금리", "ECOS 722Y001/0101000(검증: 2024-12=3.0). 변경일까지 ffill.",
           "kr_macro", FREQ_IRREGULAR, 0, TIER_REQUIRED,
           (("ecos", {"stat_code": "722Y001", "cycle": "D", "item_code1": "0101000"}),)),
    Series("한국 소비자물가지수", "ECOS 901Y009/0=총지수(검증 ~114). 발표일 +1개월 시프트.",
           "kr_macro", FREQ_MONTHLY, 0, TIER_OPTIONAL,
           (("ecos", {"stat_code": "901Y009", "cycle": "M", "item_code1": "0"}),)),
    Series("수출금액지수", "ECOS 403Y001/*AA=총지수. 반도체 수출 사이클.",
           "kr_macro", FREQ_MONTHLY, 0, TIER_OPTIONAL,
           (("ecos", {"stat_code": "403Y001", "cycle": "M", "item_code1": "*AA"}),)),
    Series("경기선행지수", "ECOS 901Y067/I16E 순환변동치. ⚠ revision(vintage 미확보 시 ML 제외 권고).",
           "kr_macro", FREQ_MONTHLY, 0, TIER_OPTIONAL,
           (("ecos", {"stat_code": "901Y067", "cycle": "M", "item_code1": "I16E"}),)),
    Series("산업생산지수", "ECOS 901Y033/A00+item2=2(계절조정). ⚠ revision.",
           "kr_macro", FREQ_MONTHLY, 0, TIER_OPTIONAL,
           (("ecos", {"stat_code": "901Y033", "cycle": "M", "item_code1": "A00", "item_code2": "2"}),)),
]


# ──────────────────────────────────────────────────────────────────────────
#  종목 컬럼 (종가/거래량) — 한글 종목명 기반 (정답지 패턴)
# ──────────────────────────────────────────────────────────────────────────
def stock_close_col(code: str) -> str:
    """종목 종가 컬럼명 = 한글 종목명 (= TARGET_COLUMNS 원소)."""
    return CODE_TO_NAME[code]


def stock_volume_col(code: str) -> str:
    """종목 거래량 컬럼명 = '<종목명>_거래량'."""
    return f"{CODE_TO_NAME[code]}_거래량"


STOCK_CODES: list[str] = [code for code, _, _ in KR_UNIVERSE]
STOCK_CLOSE_COLS: list[str] = [stock_close_col(c) for c in STOCK_CODES]
STOCK_VOLUME_COLS: list[str] = [stock_volume_col(c) for c in STOCK_CODES]


# ── 컬럼 그룹 헬퍼 ─────────────────────────────────────────────────────────
MACRO_MARKET_KEYS: list[str] = [s.key for s in MACRO_MARKET_SERIES]
SERIES_BY_KEY: dict[str, Series] = {s.key: s for s in MACRO_MARKET_SERIES}

# 일간(가격) 컬럼: ffill 연속 한도 적용 대상.
DAILY_FFILL_KEYS: list[str] = (
    [s.key for s in MACRO_MARKET_SERIES if s.freq == FREQ_DAILY]
    + STOCK_CLOSE_COLS
    + STOCK_VOLUME_COLS
)
# 저빈도(거시) 컬럼: 발표일까지 ffill(한도 미적용).
SLOW_FFILL_KEYS: list[str] = [s.key for s in MACRO_MARKET_SERIES if s.freq in SLOW_FREQS]

ALL_DATA_COLUMNS: list[str] = MACRO_MARKET_KEYS + STOCK_CLOSE_COLS + STOCK_VOLUME_COLS

# 날짜 축은 달력일(주말 포함, 정답지 방식). 주말/공휴일은 ffill 로 연장한다.
# 연속 ffill 길이가 이 값(일)을 초과하면 '의심 데이터 갭'으로 경고(정상 연휴는 통과).
WARN_FFILL_RUN_DAYS = 10
