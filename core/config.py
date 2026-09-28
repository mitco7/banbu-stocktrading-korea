"""애플리케이션 설정 로더.

`.env` 파일과 OS 환경변수를 pydantic-settings 로 읽어 타입 안전한 Settings 객체로 노출한다.
가중치·임계값 등 매직넘버 외부화의 '단일 진입점' 역할을 한다(CLAUDE.md §2.3).

시크릿은 코드에 하드코딩하지 않으며 기본값은 빈 문자열이다. 비어 있어도 앱은 기동되고,
실제로 해당 외부 서비스를 호출하는 시점에 각 클라이언트가 누락을 검증/실패시킨다.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 는 CWD 가 아니라 프로젝트 루트 기준 절대경로로 찾는다.
# (systemd 등에서 WorkingDirectory 누락 시 '빈 자격증명으로 정상 기동'하는 사고 방지)
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── 한국투자증권 KIS OpenAPI ──────────────────────────
    # 공개 엔드포인트는 기본값으로 제공(시크릿 아님). .env 로 덮어쓸 수 있다.
    KIS_BASE_URL: str = "https://openapivts.koreainvestment.com:29443"  # 모의투자
    KIS_REAL_URL: str = "https://openapi.koreainvestment.com:9443"      # 실전
    # 모의/실전 자격증명 분리(.env 키와 일치). 활성값은 kis_appkey 등 프로퍼티로 접근.
    KIS_MOCK_APPKEY: str = ""
    KIS_MOCK_APPSECRET: str = ""
    KIS_MOCK_CANO: str = ""      # 모의 계좌번호 앞 8자리
    KIS_REAL_APPKEY: str = ""
    KIS_REAL_APPSECRET: str = ""
    KIS_REAL_CANO: str = ""      # 실전 계좌번호 앞 8자리
    KIS_ACNT_PRDT_CD: str = "01" # 계좌번호 뒤 2자리(상품코드, 보통 01)
    KIS_USE_MOCK: bool = True    # 기본값은 반드시 모의투자(CLAUDE.md §2.4)

    # ── Supabase (PostgreSQL) ─────────────────────────────
    # 서버 전용 키 — service_role(RLS 우회). 이 값은 **절대 외부로 나가면 안 된다**
    # (Kaggle 노트북 주입 금지 — sql/013 RLS 도입 후 전권 키가 됨).
    SUPABASE_URL_KOREA: str = ""
    SUPABASE_KEY_KOREA: str = ""
    # Kaggle 노트북에 평문 주입되는 키 — anon(공개용). RLS 정책(sql/013)으로
    # market_data 읽기 + predicted_stocks/stock_analysis_results 갱신만 허용된다.
    # 비어 있으면 ml_trigger 가 push 자체를 거부(fail-close — service_role 유출 방지).
    SUPABASE_KAGGLE_KEY: str = ""

    # ── 거시지표 ──────────────────────────────────────────
    ECOS_API_KEY: str = ""   # 한국은행 ECOS
    FRED_API_KEY: str = ""   # 미국 거시 일부

    # ── LLM / 알림 ────────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    SLACK_WEBHOOK_URL: str = ""
    # Slack 발송 최소 레벨(info/warn/error). 웹훅 URL 이 비어 있으면 레벨 무관 발송 안 함.
    SLACK_NOTIFY_LEVEL: str = "info"

    # ── API 인증 ──────────────────────────────────────────
    # 비어 있으면 인증 비활성(로컬 개발 전용 — 기동 시 경고). 설정 시 /health 외 전 라우트에
    # X-API-Key 헤더 일치 요구. AWS 등 외부 배포 시 반드시 설정(§2.6).
    API_AUTH_KEY: str = ""

    # ── Kaggle (ML 학습) ──────────────────────────────────
    KAGGLE_USERNAME: str = ""           # 실제 Kaggle 계정 username(토큰 이름 아님)
    KAGGLE_API_TOKEN: str = ""          # 신형 Access Token("KGAT_" 접두사) — 단독 인증
    KAGGLE_KEY: str = ""                # 기존 32자리 API 키(KAGGLE_USERNAME 과 함께 사용)
    KAGGLE_KERNEL_SLUG: str = "stock-prediction-korea"  # 커널 slug
    KAGGLE_NOTEBOOK_DIR: str = "kaggle_notebook"        # 노트북 폴더(프로젝트 루트 상대)

    # ── 기능 플래그 ───────────────────────────────────────
    USE_SCORING_V2: bool = True

    # ── 데이터 수집 ───────────────────────────────────────
    # DB가 비었을 때 증분 수집의 폴백 시작일(최초 백필 시작일과 동일하게 유지).
    BACKFILL_START_DATE: str = "2010-01-01"
    # 앱 기동 시 자동 증분 수집 여부(개발 reload 환경에서는 false 권장).
    COLLECT_ON_STARTUP: bool = True
    # 수급(외국인·기관) 합산 기본 거래일 수.
    FLOW_LOOKBACK_DAYS: int = 5
    # 수급 점수 정규화 스케일: ratio(=순매수/거래대금)를 tanh(ratio/scale)로 -1~+1 squash.
    # 대형주 N일 순매수/거래대금은 보통 ±1~5%라 scale은 그 대역이어야 신호가 산다(stock-quant).
    # ⚠ 잠정값(백테스트로 보정 필요). 궁극적으로는 종목별 rolling z-score(분포 기준)로 전환 권장.
    FLOW_SCORE_SCALE: float = 0.03
    # 유동성 필터: lookback 합산 거래대금이 이 값 미만이면 수급 점수 산출 제외(fail-close).
    FLOW_MIN_TRADE_VALUE: float = 5e10  # 500억(원)
    # 수급 데이터 소스: "naver"(로그인 불필요) / "pykrx"(KRX 로그인 필요) / "auto"(naver→pykrx).
    FLOW_SOURCE: str = "naver"

    # ── 매수 후보(기술적 지표 + 결합) ─────────────────────
    # 1차 후보 필터: ML 예측 상승률(stock_analysis_results."Rise Probability (%)" =
    #   (예측가-현재가)/현재가×100, 확률 아님·CLAUDE.md §2.2) 이 값(%) 이상인 종목만.
    RECOMMEND_MIN_RISE_PCT: float = 3.0
    # 지표 계산용 일봉 조회 기간(달력일). SMA50 + Wilder 워밍업 충분히 확보.
    RECOMMEND_HISTORY_DAYS: int = 250
    # 이동평균(골든크로스 = 단기 > 장기).
    SMA_SHORT: int = 20
    SMA_LONG: int = 50
    # RSI(Wilder).
    RSI_PERIOD: int = 14
    RSI_OVERBOUGHT: float = 70.0
    RSI_OVERSOLD: float = 30.0
    # MACD(빠른/느린/시그널). 매수신호 = MACD > Signal.
    MACD_FAST: int = 12
    MACD_SLOW: int = 26
    MACD_SIGNAL: int = 9
    # ATR(Wilder) — 변동성. 익절/손절 배수는 주문 단계(다음 스텝)에서 사용.
    ATR_PERIOD: int = 14
    # ADX(Wilder) — 추세 강도. 이 값 이상이면 '추세 있음'으로 표기.
    ADX_PERIOD: int = 14
    ADX_TREND_MIN: float = 25.0
    # 거래량비율 = 오늘 거래량 / 직전 N거래일 평균 거래량.
    VOLUME_RATIO_WINDOW: int = 5
    # 지표 산출 최소 거래일 봉수. SMA_LONG + ADX(2중 Wilder 평활) 워밍업 충분치 못하면 후보 제외.
    RECOMMEND_MIN_BARS: int = 60
    # ML 분석 결과(stock_analysis_results) 허용 신선도(일). 산출 후 이 일수 초과면 stale → 후보 0건(fail-close).
    RECOMMEND_ML_MAX_AGE_DAYS: int = 3
    # 종목당 FDR 일봉 조회 wall-clock 타임아웃(초). 초과 시 그 종목만 제외(전체 행 방지).
    RECOMMEND_FDR_TIMEOUT_SEC: float = 20.0

    # ──────────────────────────────────────────────────────────────────────
    #  종합점수 / 하드필터 / VIX 게이트 (Step 5 — scoring_service)
    #  ⚠ 아래 값은 전부 '자의적 도메인 시작값'. 백테스트/모의투자로 보정 필요(CLAUDE.md §2.4).
    # ──────────────────────────────────────────────────────────────────────
    # ── 하드필터(점수 전 제외, fail-close). 점수로 되살리지 않는다. ──
    RSI_HARD_MAX: float = 82.0          # RSI 초과 시 극과열 추격 금지 → 제외
    EXT20_MAX: float = 0.25             # 이격도 (close-sma20)/sma20 초과 시 단기과열·갭다운 위험 → 제외
    ATR_PCT_MAX: float = 20.0           # atr_pct 초과 시 극단 변동성(거래정지복귀·이벤트) → 제외
    TECH_PREFILTER_MIN: int = 2         # (골든크로스/RSI매수권/MACD매수) 중 최소 충족 개수
    ASOF_MAX_GAP_DAYS: int = 2          # ML as-of/지표/수급 기준일 최대 격차(영업일). 초과 시 fail-close

    # ── 종합점수 가중치(합=1.0). 절대·유계 서브스코어 가중합(횡단면 z-score 금지). ──
    W_ML: float = 0.30
    W_TECH: float = 0.30
    W_FLOW: float = 0.25
    W_VOL: float = 0.15

    # ── ml_sub: 예측 상승률 saturating 변환 ──
    ML_RISE_CAP: float = 15.0           # 14일 +15% 이상은 만점(과적합 의심 구간 saturate)
    # ML 예측 기간(거래일). **kaggle_notebook/predict.py 의 FORECAST_HORIZON 과 반드시 일치**
    # — 알림 문구 표기에만 쓰지만 값이 어긋나면 잘못된 기간을 안내하게 된다.
    ML_FORECAST_DAYS: int = 14

    # ── tech_sub 내부 가중(합=1.0) + cap ──
    TECH_W_GC: float = 0.30             # 골든크로스
    TECH_W_MACD: float = 0.30           # MACD 매수
    TECH_W_RSI: float = 0.20            # RSI 건강도(보조)
    TECH_W_ADX: float = 0.20            # ADX 추세세기×방향가드(보조)
    ADX_CAP: float = 40.0
    # RSI 건강도 매핑 경계.
    RSI_OVERSOLD_FLOOR: float = 30.0    # 이하 = 딥 과매도(0.5, 떨어지는 칼날 주의)
    RSI_FULL_LO: float = 40.0           # 이상부터 만점 시작(rsi_pt 전용)
    RSI_FULL_HI: float = 75.0           # 이하까지 만점(건강한 매수권·눌림목)
    # (RSI_HARD_MAX=82 위에서 정의 — 75~82 는 1.0→0.6 선형 감점, 82 초과는 하드필터 제외)
    # 기술 사전필터의 'RSI 매수권' 하한/상한 — 점수 만점선(RSI_FULL_*)과 분리해 따로 튜닝.
    RSI_PREFILTER_LO: float = 40.0
    RSI_PREFILTER_HI: float = 75.0

    # ── flow_sub: 외국인/기관 합치도 ──
    CONFLUENCE_MULT: float = 0.7        # 외국인·기관 이견(부호 반대) 시 신뢰도 배수(죽이진 않음)

    # ── vol_sub: 거래량비율 saturating ──
    VOL_RATIO_CAP: float = 3.0          # 3배 이상 만점(상단 cap — 패닉·블록딜 단발 이벤트 무한가산 방지)

    # ── VIX 시장 변동성 게이트(VKOSPI 없음 → VIX 대체, 반드시 T-1 shift) ──
    VIX_CAUTION: float = 25.0           # 이상이면 보수모드(매수 임계 상향)
    VIX_BLOCK: float = 30.0             # 이상이면 신규매수 전면 차단
    VIX_CAUTION_MIN_RISE_PCT: float = 5.0  # 보수모드 시 상향된 최소 예측 상승률(3%→5%)

    # ── 일일 파이프라인 오케스트레이션(/pipeline/run — Step 1~5 일괄 실행) ──
    # ML(Kaggle) 완료 대기 폴링 간격(초).
    PIPELINE_ML_POLL_INTERVAL_SEC: float = 30.0
    # ML 완료 대기 상한(초). 초과 시 파이프라인 중단(fail-close — 이후 단계 진행 금지).
    PIPELINE_ML_TIMEOUT_SEC: float = 900.0  # 15분

    # ── 스케줄러(Step 7 — services/scheduler, 시각은 전부 KST "HH:MM") ────
    SCHEDULER_ENABLED: bool = True
    # 일일 파이프라인(수집→ML→매수결정) 실행 시각. 장 시작 전 완료되도록 새벽 권장.
    # ⚠ 06:00 이전 설정 금지 권장: 미국 정규장 마감이 겨울(EST, 11~3월) 기준 KST 06:00 정각이라
    #   그 시각 실행 시 전일(T-1) 행의 VIX·미국지수 컬럼이 아직 소스에 없어 전전일 값 ffill 로
    #   저장·고착된다(upsert 가 빈 컬럼만 채움) → VIX[T-1] 게이트가 사실상 T-2 로 동작.
    #   미국 마감 후 지수 확정(+15분)과 소스 반영 여유까지 둔 07:00 을 기본값으로 한다.
    SCHEDULE_PIPELINE_TIME: str = "07:00"
    # ⚠ 새벽 스케줄에서 force_market=True 금지 수준 위험: 06시엔 당일 시세가 소스에 없어
    #   당일 행이 전일값 ffill 로 만들어지고, market_data upsert 는 빈 컬럼만 채우는 정책이라
    #   이후 실제 당일 종가가 영구히 덮어써지지 못한다(데이터 1일 시프트 오염).
    #   당일 포함 수집이 필요하면 장 마감 후 수동 /market/update?force=true 로만.
    SCHEDULE_PIPELINE_FORCE_MARKET: bool = False
    # 매수 집행 시각(장 시작 1시간 후). BUY_ORDER_START~END 시간창 안이어야 실제 주문된다.
    SCHEDULE_BUY_TIME: str = "10:00"
    # 장중 매도 감시 주기(초). 장외에는 run_sell_cycle 내부 가드가 즉시 반환.
    SCHEDULE_SELL_INTERVAL_SEC: int = 60
    # 장 마감 후 체결 정합성 동기화 시각(마감 직전 체결의 '체결 확인' 알림 지연 해소).
    SCHEDULE_RECONCILE_TIME: str = "15:40"
    # 스케줄 잡 실패 Slack 알림 최소 간격(초) — 1분 주기 잡의 연속 실패 스팸 방지.
    SCHEDULE_ERROR_NOTIFY_COOLDOWN_SEC: int = 600

    # ── KRX 캘린더 ────────────────────────────────────────
    # core/market_calendar.py 의 연도별 휴장일에 추가할 임시 휴장일(쉼표 구분 ISO 날짜).
    # 예: 임시공휴일 등 '종일 휴장' 공지 대응용. "2026-12-03,2027-03-03"
    # ⚠ 수능일 등 '지연개장일'(장은 열림, 시간만 10:00~16:30 이동)은 등록 금지 — 등록하면
    #   그날 보유 포지션 손절 감시(run_sell_cycle)와 reconcile 까지 통째로 꺼진다.
    #   지연개장 세션 표현은 미지원: 그런 날은 수동 감시로 대응(개선 과제).
    KRX_HOLIDAYS_EXTRA: str = ""

    # ──────────────────────────────────────────────────────────────────────
    #  KIS 매매 / 익절·손절 (Step 6 — trading_service)
    #  ⚠ 배수·임계는 자의적 시작값. 모의투자 백테스트로 보정 전 실전 전환 금지(CLAUDE.md §2.4).
    # ──────────────────────────────────────────────────────────────────────
    # 주문 집행 스위치. false 면 수집→ML→점수→AI 검토→Slack 추천까지 그대로 돌리되
    # **KIS 주문을 일절 보내지 않는다**(매수·매도·정합성 동기화 모두 스킵, 읽기 호출도 없음).
    # 모의투자 계좌 만료 등으로 주문이 거부되는 기간에 분석 파이프라인만 유지할 때 사용
    # (2026-09-23 모의계좌 만료로 주문 거부 40910000 관측 → 2026-09-28 부터 false 운용).
    # 복구하려면 새 모의투자 계좌를 .env 에 반영한 뒤 true 로 되돌린다.
    TRADING_ENABLED: bool = True
    # 포지션 사이징.
    POSITION_PCT: float = 0.10          # 종목당 가용자금 비중 상한(자산의 10%)
    PRICE_LIMIT_PCT: float = 0.30       # 상·하한가(±30%) — 지정가가 이 범위 벗어나면 거부
    # KIS API 호출 페이싱(초당 건수). 모의투자 공식 제한 2건/초 — 실전은 .env 로 상향 가능.
    KIS_RATE_LIMIT_PER_SEC: float = 2.0
    # KIS HTTP 읽기 타임아웃(초). 모의서버가 장 후반(14:30~15:30) 10초 초과 응답하는 패턴 관측
    # (2026-09 운영) → 15초. 주문(POST)도 공유 — 타임아웃 시 KisOrderUncertainError 로 안전 처리.
    KIS_TIMEOUT_SEC: float = 15.0
    # KIS 연결(TCP/TLS) 타임아웃(초) — 읽기와 분리. 정상 시 EC2→KIS 접속은 수 ms(실측 4ms)라
    # 5초면 충분하다. 모의서버 장애일(2026-09-16·18)엔 접속 단계에서 15초씩 묶여 매도 사이클이 늘어졌다.
    KIS_CONNECT_TIMEOUT_SEC: float = Field(5.0, gt=0)
    # 매도 감시(1분 주기) 시세 조회의 총 시도 횟수(최초 포함). 60초 뒤 다음 사이클이 어차피
    # 재확인하므로 레이트리밋(EGW00201) 1회 회복분만 남긴다 — 3회면 장애일 종목당 ~50초가 걸려
    # 사이클이 밀리고(9/18 사이클 176회 스킵) 이미 느린 서버에 재시도 부하까지 더한다.
    SELL_QUOTE_ATTEMPTS: int = Field(2, ge=1)  # 0이면 retry 가 호출조차 안 해 손절 감시가 통째로 꺼진다
    # 매도 감시 공백 알림 임계(분): 한 종목의 시세 확인이 이 시간 이상 연속 실패하면 warn 1건,
    # 복구 시 info 1건. 사이클 단위 일시 오류(모의서버에서 흔함)는 알리지 않는다.
    SELL_STALE_ALERT_MIN: int = Field(5, ge=1)
    # 매도 주문 확정 거부(KisError) Slack 알림 최소 간격(초) — 같은 종목·같은 사유의 반복 거부를
    # 이 간격당 1건으로 묶는다. 계좌 단위 거부(예: 40910000 모의투자 주문 불가 계좌)는 사람이
    # 고치기 전까지 매 사이클 동일하게 실패하므로 1분마다 알리면 채널이 마비된다. 재시도 자체는
    # 손절 안전망이라 유지(주문 1건/분).
    SELL_REJECT_NOTIFY_COOLDOWN_SEC: int = Field(1800, ge=0)
    # 매도 사이클 내 장중 reconcile(잔고 동기화) 최소 간격(초). 매 60초 잔고 조회가
    # 모의서버 부하·레이트리밋의 주요인이라 완화 — 체결 전이 반영이 최대 이 간격만큼
    # 지연되지만(기본 5분) 14일 스윙 TP/SL 에 영향 미미. 15:40 마감 reconcile 은 별도.
    SELL_RECONCILE_INTERVAL_SEC: int = 300
    # 매수 주문 허용 시간창(KST). 동시호가 시세 왜곡 회피를 위해 시초가 확정 후 시작 권장.
    BUY_ORDER_START: str = "09:01"
    BUY_ORDER_END: str = "15:00"
    # ATR 익절/손절 배수(CLAUDE.md §2.4 — 2026-08 개정: 익절3.0/손절2.5, 손절 우선).
    # 백테스트(10종목×2년, 장중터치)상 4.0이 근소 우세(+0.35%p)였으나 상승장 표본 편향이 있고,
    # 3~4ATR 사이 정체 구간(전체 9.1%)에선 조기익절이 81.7% 확률로 유리해 사용자 결정으로 3.0 채택.
    # 모의투자 실측(상승/횡보/하락 분리)으로 재보정 대상.
    TP_ATR_MULT: float = 3.0
    SL_ATR_MULT: float = 2.5
    # 시간 청산: 매수 후 이 영업일 경과 시 TP/SL 미도달이어도 청산(ML 예측 14일 만료).
    TIME_STOP_DAYS: int = 14
    # 매도 기술신호 임계(자의적 시작값).
    SELL_RSI_OVERBOUGHT: float = 82.0   # 이 위면 과열 매도신호(매수 RSI_HARD_MAX 와 정합)
    PANIC_VOL_MULT: float = 2.0         # 패닉셀: 거래량 평소 N배 이상
    PANIC_DROP_PCT: float = -3.0        # + 당일 등락률 이 % 이하
    SELL_FLOW_BAD: float = -0.15        # 신호 2개 + flow_score 이 값 미만이면 매도
    SELL_SIGNAL_THRESHOLD: int = 3      # 신호 N개 이상이면 매도
    # 매도 시장변동성 가중(VKOSPI 제거 → VIX[T-1] 재사용). VIX_CAUTION/VIX_BLOCK 임계 공유.
    SELL_VIX_CAUTION_SIGNALS: int = 2   # VIX[T-1]>=VIX_CAUTION + 신호 이 개수 이상 → 매도
    SELL_VIX_BLOCK_SIGNALS: int = 1     # VIX[T-1]>=VIX_BLOCK + 신호 이 개수 이상 → 매도

    @property
    def kis_active_url(self) -> str:
        """현재 모드(모의/실전)에 해당하는 KIS 기준 URL."""
        return self.KIS_BASE_URL if self.KIS_USE_MOCK else self.KIS_REAL_URL

    @property
    def kis_environment(self) -> str:
        """KIS 환경 식별자(`PAPER`/`PROD`) — 토큰 격리 등에 사용."""
        return "PAPER" if self.KIS_USE_MOCK else "PROD"

    @property
    def kis_appkey(self) -> str:
        return self.KIS_MOCK_APPKEY if self.KIS_USE_MOCK else self.KIS_REAL_APPKEY

    @property
    def kis_appsecret(self) -> str:
        return self.KIS_MOCK_APPSECRET if self.KIS_USE_MOCK else self.KIS_REAL_APPSECRET

    @property
    def kis_cano(self) -> str:
        """현재 모드 계좌번호 앞 8자리."""
        return self.KIS_MOCK_CANO if self.KIS_USE_MOCK else self.KIS_REAL_CANO


@lru_cache
def get_settings() -> Settings:
    """프로세스 전역 단일 Settings 인스턴스(캐시)."""
    return Settings()


# 편의 접근용 모듈 전역. 의존성 주입이 필요한 곳에서는 get_settings() 사용 권장.
settings = get_settings()


# 동점 정렬 tie-breaker (결정적·멱등 — 매번 같은 순서). (필드, ascending) 순서대로 적용.
# 1)종합점수↓ 2)atr_pct↑(저변동 선호) 3)예측상승률↓ 4)flow_score↓ 5)ticker↑
# ⚠ 시작값. atr tie-breaker 가 저변동주를 미세하게 밀어올리니 후보 쏠림 보이면 순서 조정(백테스트).
SCORE_TIE_BREAKERS: tuple[tuple[str, bool], ...] = (
    ("total_score", False),
    ("atr_pct", True),
    ("predicted_rise_pct", False),
    ("flow_score", False),
    ("ticker", True),
)


# 한국 주식 호가단위(2023-01 KRX 개편 기준, KOSPI/KOSDAQ 공통). (가격_미만, 틱). 마지막은 그 이상.
# ⚠ KRX 호가단위 규정 변경 시 갱신 필요(구현 직전 재확인). 매직넘버 외부화(§2.3).
HOGA_TICKS: tuple[tuple[float, int], ...] = (
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
    (float("inf"), 1_000),
)
