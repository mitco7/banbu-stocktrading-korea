# ARCHITECTURE.md — 한국 주식 자동매매 시스템 아키텍처

## 1. 전체 아키텍처 관점

```
┌─────────────────────────────────────────────────────────────────┐
│                        Scheduler (APScheduler / cron)            │
│        (시간대별 잡 트리거: 수집 → 예측 → 점수 → 검토 → 매매)      │
└─────────────────────────────────────────────────────────────────┘
              │                                  │
              ▼                                  ▼
       ┌───────────────┐                ┌───────────────┐
       │   Services    │                │      API      │
       │ (비즈니스로직) │ ◄────────────► │ (FastAPI:      │
       │               │                │  헬스체크/조회) │
       └───────┬───────┘                └───────┬───────┘
               │                                │
               ▼                                ▼
        ┌─────────────────────────────────────────────┐
        │              Core (설정/공통)                │
        │   config, exceptions, constants, types      │
        └─────────────────────────────────────────────┘
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
       ┌──────────┐     ┌──────────┐     ┌──────────┐
       │   Utils  │     │    DB    │     │ External │
       │ (지표계산,│     │(Supabase │     │   APIs   │
       │  로거,   │     │ Postgres │     │ (KIS,    │
       │  ffill등) │     │ Client)  │     │  ECOS등) │
       └──────────┘     └──────────┘     └──────────┘
```

---

## 2. 폴더 구조 (Layered Architecture)

```
banbu-stocktrading-korea/
├── PLAN.md
├── ARCHITECTURE.md
├── README.md                       # (Step 1에서 생성)
├── .env.example
├── .gitignore
├── requirements.txt
│
├── api/                            # FastAPI 엔드포인트 (헬스체크, 조회용)
│   ├── __init__.py
│   ├── main.py                     # FastAPI 앱 진입점
│   ├── routers/
│   │   ├── __init__.py             # 라우터 통합(api_router) — main 에서 일괄 등록
│   │   ├── health.py               # /health, /health/external
│   │   ├── market.py               # /market/prices, /market/macro
│   │   ├── predictions.py          # /predictions/{date}
│   │   ├── recommendations.py      # /recommendations/{date}
│   │   └── trades.py               # /trades, /trades/open
│   └── dependencies.py             # DI: DB 세션, 인증 등
│
├── services/                       # 비즈니스 로직 (단계별 모듈)
│   ├── __init__.py
│   ├── collectors/
│   │   ├── macro_collector.py      # ECOS 거시지표
│   │   ├── index_collector.py      # KOSPI/KOSDAQ/VKOSPI/환율
│   │   ├── ohlcv_collector.py      # 25종목 일봉
│   │   ├── flow_collector.py       # 외국인/기관 수급
│   │   └── universe_selector.py    # 시총·거래대금 상위 25 후보 산출(수동 승인용)
│   ├── indicators/
│   │   ├── technical.py            # SMA, RSI, MACD, ATR, ADX, 거래량비율, 골든크로스
│   │   └── flow_score.py           # 수급 점수 (-1~+1)
│   ├── ml/
│   │   ├── predictor.py            # 학습된 가중치 로드 + 일일 추론
│   │   ├── features.py             # 90일 윈도우 피처 빌더
│   │   └── model_loader.py         # Kaggle 산출물 로드/버전관리
│   ├── scoring/
│   │   ├── zscore.py               # 후보군 내 z-score 표준화
│   │   └── aggregator.py           # 가중합 + VKOSPI 필터 + 후보 선정
│   ├── llm/
│   │   ├── reviewer.py             # Claude에 후보 검토 요청
│   │   ├── prompts.py              # 시스템/유저 프롬프트 템플릿
│   │   └── parser.py               # 응답 파싱 (승인/거부 + 사유)
│   ├── trading/
│   │   ├── kis_client.py           # KIS API 래퍼 (시세/주문/체결/잔고)
│   │   ├── token_manager.py        # KIS 액세스 토큰 24h 관리
│   │   ├── buyer.py                # 매수 실행 + ATR 익절/손절 설정
│   │   ├── seller.py               # 장중 매도 감시/실행
│   │   └── position_manager.py     # 보유 종목 상태/리스크 관리
│   ├── notifications/
│   │   └── slack_notifier.py       # Slack Webhook 발송
│   └── scheduler/
│       └── jobs.py                 # APScheduler 잡 정의 (시간대별 트리거)
│
├── utils/                          # 순수 유틸 (외부 의존 최소)
│   ├── __init__.py
│   ├── logger.py                   # 구조화 로깅 (stdout + 파일)
│   ├── dataframe.py                # ffill 전용 결측 처리 헬퍼
│   ├── datetime_kst.py             # KST 시간 처리, 영업일 캘린더
│   ├── retry.py                    # 외부 API 재시도 데코레이터
│   └── crypto.py                   # 토큰 등 민감정보 마스킹
│
├── core/                           # 공통 코어 (config, 예외, 상수)
│   ├── __init__.py
│   ├── config.py                   # pydantic-settings 기반 환경변수 로더
│   ├── constants.py                # 상수 (KST 시간, 가중치 디폴트 등)
│   ├── exceptions.py               # 커스텀 예외 (KISError, DataGapError 등)
│   ├── types.py                    # TypedDict / Pydantic 모델 (Signal, Position 등)
│   └── healthcheck.py              # 외부 서비스 핑 진단 스크립트
│
├── db/                             # DB 접근 레이어
│   ├── __init__.py
│   ├── client.py                   # Supabase 클라이언트 래퍼
│   ├── migrations/                 # SQL 마이그레이션 파일 (버전순)
│   │   ├── 001_init_market.sql
│   │   ├── 002_predictions.sql
│   │   ├── 003_trading.sql
│   │   └── 004_llm_tokens.sql
│   └── repositories/               # 테이블별 CRUD 함수
│       ├── market_repo.py
│       ├── flow_repo.py
│       ├── prediction_repo.py
│       ├── recommendation_repo.py
│       ├── trade_repo.py
│       ├── llm_repo.py
│       └── token_repo.py
│
├── ml/                             # Kaggle 학습 자산 (코드/노트북)
│   ├── kaggle/
│   │   └── train_transformer.ipynb # Kaggle GPU 학습 노트북
│   └── weights/                    # 다운로드된 가중치 (gitignore)
│
├── scripts/                        # 운영 스크립트 (백필/일회성)
│   ├── backfill_market.py
│   ├── backfill_flow.py
│   └── manual_universe_update.py   # 운영자 수동 종목 교체
│
└── tests/                          # pytest 단위/통합 테스트
    ├── unit/
    └── integration/
```

### 레이어 의존 규칙
- `api/` → `services/` → `db/`, `utils/`, `core/`
- `services/` 끼리는 **단방향 의존만** (예: `trading` → `notifications` OK, 역방향 금지)
- `utils/`, `core/`는 어떤 레이어도 import 가능 (단, 외부 SDK 의존 최소)
- `db/repositories`만 `db/client` 사용, 다른 레이어는 repository만 호출

---

## 3. DB 테이블 설계 (Supabase / PostgreSQL)

> 모든 테이블은 `created_at TIMESTAMPTZ DEFAULT NOW()` 자동 컬럼을 가진다.
> 시계열 테이블은 (종목코드, 거래일) 또는 (지표코드, 기준일) **복합 유니크 인덱스**를 둔다.

### 3.1 시장 데이터 계열

#### `macro_indicators` — 거시지표 (ECOS + 일부 미국)
| 컬럼 | 타입 | 설명 |
|---|---|---|
| id | BIGSERIAL PK | |
| indicator_code | TEXT | ECOS 통계코드 또는 내부코드 (예: `BOK_BASE_RATE`, `US_CPI`) |
| indicator_name | TEXT | 사람이 읽는 이름 |
| ref_date | DATE | 지표 기준일 |
| value | NUMERIC | 값 |
| source | TEXT | `ECOS` / `FDR` / `MANUAL` |
| **UNIQUE** | (indicator_code, ref_date) | |

#### `index_prices` — 지수/환율 일봉 (KOSPI, KOSDAQ, VKOSPI, USD/KRW 등)
| 컬럼 | 타입 |
|---|---|
| id, symbol(TEXT), trade_date(DATE), open, high, low, close, volume(NULL허용) | NUMERIC |
| **UNIQUE** (symbol, trade_date) | |

#### `stock_universe` — 매매 대상 25종목 (수동 관리)
| 컬럼 | 타입 | 설명 |
|---|---|---|
| ticker | TEXT PK | 종목코드 (예: `005930`) |
| name | TEXT | 종목명 |
| market | TEXT | `KOSPI` / `KOSDAQ` |
| is_active | BOOLEAN | 현재 운용 대상 여부 |
| added_at | TIMESTAMPTZ | 편입 시각 |
| removed_at | TIMESTAMPTZ NULL | 제외 시각 (이력 보존) |
| note | TEXT | 운영자 메모 |

#### `stock_prices` — 25종목 일봉
| 컬럼 | 타입 |
|---|---|
| id, ticker(TEXT FK), trade_date(DATE), open, high, low, close, volume, value(거래대금) | NUMERIC |
| **UNIQUE** (ticker, trade_date) | |

#### `investor_flows` — 외국인/기관 수급 (감성 대체)
| 컬럼 | 타입 | 설명 |
|---|---|---|
| id, ticker(TEXT), trade_date(DATE) | — |
| foreign_net | NUMERIC | 외국인 순매수 금액 |
| institution_net | NUMERIC | 기관 순매수 금액 |
| individual_net | NUMERIC | 개인 순매수 (참고용) |
| flow_score | NUMERIC | -1~+1로 정규화된 점수 |
| **UNIQUE** (ticker, trade_date) | |

### 3.2 시그널 / 의사결정 계열

#### `technical_signals` — 일별 기술적 지표 계산 결과
| 컬럼 | 타입 |
|---|---|
| id, ticker, trade_date | — |
| sma_5, sma_20, sma_60, rsi_14, macd, macd_signal, atr_14, adx_14, volume_ratio | NUMERIC |
| golden_cross | BOOLEAN |
| **UNIQUE** (ticker, trade_date) | |

#### `ml_predictions` — Transformer 14일 후 가격 예측
| 컬럼 | 타입 | 설명 |
|---|---|---|
| id, ticker, predict_date(DATE) | — | 예측 수행일 |
| target_date | DATE | 14영업일 후 목표일 |
| predicted_price | NUMERIC | |
| predicted_return | NUMERIC | 현재가 대비 예측 수익률 |
| model_version | TEXT | 가중치 버전 (예: `v2026-05-20`) |
| input_window | INT | 입력 윈도우 길이 (=90) |
| **UNIQUE** (ticker, predict_date, model_version) | |

#### `recommendations` — 종합 점수 및 후보 선정 결과
| 컬럼 | 타입 | 설명 |
|---|---|---|
| id, ticker, ref_date(DATE) | — | |
| ml_z, tech_z, flow_z | NUMERIC | 각 팩터 z-score |
| weighted_score | NUMERIC | 가중합 |
| passed_threshold | BOOLEAN | 임계값 통과 여부 |
| vkospi_value | NUMERIC | 그날의 VKOSPI |
| conservative_mode | BOOLEAN | VKOSPI 높아 매수 차단 여부 |
| rank | INT | 후보 내 순위 |
| **UNIQUE** (ticker, ref_date) | |

#### `llm_decisions` — Claude 검토 로그
| 컬럼 | 타입 | 설명 |
|---|---|---|
| id, ref_date(DATE), ticker | — | |
| prompt_hash | TEXT | 동일 프롬프트 캐시/추적용 |
| approved | BOOLEAN | 최종 승인 여부 |
| reason | TEXT | LLM이 제시한 사유 |
| raw_response | JSONB | 응답 원문 전체 |
| model | TEXT | 예: `claude-opus-4-7` |
| created_at | TIMESTAMPTZ | |

### 3.3 거래 계열

#### `trades` — 매수/매도 체결 기록
| 컬럼 | 타입 | 설명 |
|---|---|---|
| id, ticker, side(`BUY`/`SELL`) | — | |
| order_id | TEXT | KIS 주문번호 |
| order_type | TEXT | `MARKET` / `LIMIT` |
| quantity | INT | |
| price | NUMERIC | 체결가 |
| ordered_at, filled_at | TIMESTAMPTZ | |
| status | TEXT | `PENDING`/`FILLED`/`CANCELED`/`REJECTED` |
| is_paper | BOOLEAN | 모의/실전 구분 |
| recommendation_id | BIGINT FK NULL | 매수의 근거 추천 |
| sell_reason | TEXT NULL | `TP`/`SL`/`TECHNICAL`/`MANUAL` |

#### `positions` — 현재/과거 포지션
| 컬럼 | 타입 |
|---|---|
| id, ticker, entry_price, quantity, take_profit, stop_loss(ATR기반) | NUMERIC |
| opened_at, closed_at(NULL=보유중) | TIMESTAMPTZ |
| pnl(평가/실현손익) | NUMERIC |
| atr_at_entry | NUMERIC |
| status | `OPEN`/`CLOSED` |

### 3.4 운영 계열

#### `kis_tokens` — KIS 액세스 토큰 (24h)
| 컬럼 | 타입 |
|---|---|
| id, environment(`PAPER`/`PROD`), access_token, issued_at, expires_at | — |
| 활성 토큰은 environment별 최신 1개를 사용 | |

#### `pipeline_runs` — 파이프라인 실행 로그 (관측성)
| 컬럼 | 타입 |
|---|---|
| id, stage(`COLLECT`/`PREDICT`/`SCORE`/`LLM`/`BUY`/`SELL`), run_date, status, started_at, finished_at, error | — |

---

## 4. 기술 스택 및 선정 이유

| 영역 | 선택 | 이유 |
|---|---|---|
| **언어** | Python 3.11+ | 데이터/ML 생태계 표준, pykrx·FinanceDataReader 등 한국 라이브러리 지원 |
| **웹 프레임워크** | FastAPI | 비동기 지원, pydantic 통합, 헬스체크/조회 API에 가볍게 적합 |
| **설정** | pydantic-settings + `.env` | 타입 안전 + 12-factor 친화 |
| **스케줄러** | APScheduler (1차) → cron (운영 안정화 후) | 코드 내에서 시간대별 잡 정의가 명확, KST 처리 용이 |
| **DB** | Supabase (PostgreSQL) | 매니지드 PG + 인증/스토리지 통합, 무료 티어로 시작 가능 |
| **DB 클라이언트** | supabase-py + SQL 마이그레이션 직접 작성 | ORM 오버헤드 없이 명시적 스키마 관리 |
| **데이터 소스** | pykrx (메인) + FinanceDataReader (보조) | 한국 시장 커버리지·수급 데이터 정확도 |
| **거시지표** | 한국은행 ECOS API | 한국 거시지표 1차 공식 출처 |
| **ML 프레임워크** | PyTorch | Transformer 구현 표준, Kaggle GPU 친화 |
| **학습 환경** | Kaggle Notebook (GPU) | 무료 GPU, 재현 가능한 노트북 환경 |
| **LLM** | Anthropic Claude API | 한국어 추론·근거 제시 품질, JSON 응답 안정성 |
| **알림** | Slack Incoming Webhook | 셋업 5분, 모바일/데스크탑 즉시 알림 |
| **거래소** | KIS OpenAPI | 한국 증권사 중 가장 잘 문서화된 OpenAPI + 모의투자 지원 |
| **로깅** | 구조화 로깅(JSON) → stdout + 파일 | 추후 클라우드 이전 시 그대로 사용 |
| **테스트** | pytest | 표준 |
| **패키지 관리** | pip + requirements.txt (1차), 추후 uv/poetry 검토 | 단순함 우선 |

---

## 5. 주요 설계 원칙

### 5.1 데이터 무결성
- **결측치 처리는 ffill(전일치 보간)만 허용**. 평균/중앙값/랜덤 보간 금지.
- ffill 적용 시 **연속 ffill 일수 제한**(예: 최대 3일)을 두고, 초과 시 데이터 갭으로 분류 + Slack 알림.
- 모든 외부 API 응답은 원본을 보존(`raw_response` JSONB 컬럼 또는 별도 저장).

### 5.2 안전 우선 거래
- **모의투자(PAPER)가 기본값**. 실전(PROD) 전환은 환경변수 + 명시적 확인 절차로만 가능.
- 매수 직전 LLM 검토는 **거부권만**: 후보에 없던 종목을 LLM이 추가하는 것을 코드 레벨에서 차단.
- VKOSPI가 임계값 초과 시 신규 매수 자동 차단(보수 모드).
- 매수 시점에 ATR 기반 익절/손절을 **반드시 함께 설정**하고 DB에 기록.

### 5.3 관측성 & 감사 추적
- 모든 의사결정(예측·점수·LLM 응답)은 DB에 영속화 → 사후 백테스트/감사 가능.
- 파이프라인 단계별 시작/종료/오류를 `pipeline_runs`에 기록.
- Slack 알림은 핵심 4종(수집완료/매수결정/매수체결/매도체결) + 에러.

### 5.4 운영 거버넌스
- **종목 유니버스 변경은 항상 사람의 손**: `universe_selector`는 후보만 산출, `stock_universe` 테이블 변경은 수동 스크립트.
- KIS 토큰은 환경별로 격리(`PAPER`/`PROD`)되어 한 쪽 사고가 다른 쪽에 전이 불가.
- 모든 외부 API 호출은 재시도 + 타임아웃 + 로깅이 데코레이터로 강제.

### 5.5 단순성 & 재현성
- 일봉 기반(매도만 1분 단위) — 인프라 복잡도 최소화.
- ML 모델 가중치는 **버전 문자열로 식별**(`v2026-05-20`) → 어떤 예측이 어떤 모델에서 나왔는지 추적.
- 가중치/임계값 등 매직넘버는 모두 `core/config.py` 또는 설정 파일로 외부화.

### 5.6 점진적 확장
- 1차: 로컬 실행 + Supabase. 2차: 클라우드 VM 이전. 3차(선택): 대시보드 분리 프로젝트.
- 어떤 단계에서도 **DB 스키마와 서비스 인터페이스는 유지** → 무중단 이전 가능.
