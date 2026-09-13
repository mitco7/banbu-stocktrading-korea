# banbu-stocktrading-korea

한국 주식 시장(KOSPI/KOSDAQ) 대형주 **25종목**을 대상으로 하는 다중 팩터 기반 자동매매 백엔드.

**머신러닝 가격 예측 + 기술적 지표 + 수급(외국인/기관) + LLM(Claude) 최종 검토(거부권)** 를 결합해
매수를 결정하고, ATR 기반 익절/손절로 포지션을 관리한다.

- **시간대**: KST(Asia/Seoul), 정규장 09:00~15:30 (서머타임 없음)
- **거래 채널**: 한국투자증권(KIS) OpenAPI — **모의투자 우선**, 검증 후 실전 전환
- **운용 철학**: 안정성 > 수익률 · 자동 판단 + 수동 거버넌스 · 모든 결정은 DB에 영속화

> 설계 상세는 [`PLAN.md`](./PLAN.md)(목표·로드맵)와 [`ARCHITECTURE.md`](./ARCHITECTURE.md)(폴더 구조·DB·기술스택),
> 공통 규칙은 [`CLAUDE.md`](./CLAUDE.md)를 참고한다.

---

## 폴더 구조 (요약)

```
banbu-stocktrading-korea/
├── run.py            # 로컬 서버 진입점 (uvicorn api.main:app)
├── api/              # FastAPI 엔드포인트 (헬스체크/조회)
│   ├── main.py
│   ├── dependencies.py
│   └── routers/      # health.py 등 라우트 모듈
├── services/         # 비즈니스 로직 (수집·지표·ML·점수·LLM·매매·알림·스케줄러)
├── core/             # 설정/예외/상수 (config.py 등)
├── db/               # Supabase 클라이언트 + 마이그레이션 + repositories
├── utils/            # 순수 유틸 (logger, ffill, KST 시간, 재시도, 마스킹)
├── ml/               # Kaggle 학습 자산 / 가중치(gitignore)
├── scripts/          # 운영 스크립트 (백필·수동 유니버스 교체)
└── tests/            # pytest
```

레이어 의존 방향: `api/` → `services/` → `db/`, `utils/`, `core/` (자세한 규칙은 ARCHITECTURE.md).

---

## 설치 및 실행

### 1) 가상환경 생성 + 의존성 설치

**Windows (PowerShell)**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> Python 3.11+ 권장.

### 2) 환경변수 설정

`.env.example` 을 복사해 `.env` 를 만들고 실제 자격증명을 채운다. `.env` 는 커밋되지 않는다.

```powershell
Copy-Item .env.example .env   # macOS/Linux: cp .env.example .env
```

| 키 | 용도 |
|---|---|
| `KIS_*` | 한국투자증권 OpenAPI (모의투자 기본: `KIS_USE_MOCK=true`) |
| `SUPABASE_URL_KOREA`, `SUPABASE_KEY_KOREA` | DB(PostgreSQL) |
| `ECOS_API_KEY`, `FRED_API_KEY` | 거시지표 (한국은행 / 미국 일부) |
| `ANTHROPIC_API_KEY` | LLM 검토(Claude) |
| `SLACK_WEBHOOK_URL` | 알림 |
| `KAGGLE_*` | ML 학습 (Step 4에서 사용) |
| `USE_SCORING_V2` | 점수 로직 플래그 |

> 🔐 시크릿은 오직 `.env` 로만 관리한다. 코드·로그·커밋 어디에도 하드코딩 금지.
> 실전 거래 전환(`KIS_USE_MOCK=false`)은 명시적 의사결정 단계에서만 한다.

### 3) 서버 실행

```bash
python run.py
```

- 서버: http://localhost:8000
- 헬스체크: http://localhost:8000/health → `{"status":"ok"}`
- API 문서(Swagger): http://localhost:8000/docs

---

## 단계별 로드맵 (요약)

| Step | 내용 | 완료 기준 |
|---|---|---|
| **0** | 기획 & 아키텍처 설계 | `PLAN.md`, `ARCHITECTURE.md` 만으로 무엇을 어떤 순서로 만들지 명확 |
| **1** | 프로젝트 초기 셋팅 ← *현재* | 폴더 골격 · `.env`/`requirements`/`.gitignore` · 설정 로더 · `python run.py` 기동 |
| **2** | 데이터 수집 레이어 | ECOS/지수/일봉/수급 수집 + 백필 + 증분 스케줄러, DB 적재 |
| **3** | 기술적 지표 & 수급 점수 | SMA/RSI/MACD/ATR/ADX/골든크로스 + 수급 -1~+1 + z-score |
| **4** | ML 예측 파이프라인 | Kaggle 학습 → 가중치 버전관리 → 14일 후 예측을 DB 저장 |
| **5** | 종합 점수 & 후보 선정 | z-score 가중합 + VKOSPI 보수 모드 → `recommendations` 저장 |
| **6** | LLM 검토 레이어 | Claude 거부권 · 신규 종목 추가 금지 · 응답 원문 저장 |
| **7** | 매매 실행 & 보유 관리 | KIS 토큰/매수/매도 감시 · ATR 익절·손절 · 모의 무중단 검증 |
| **8** | 알림 / 운영 / 관측성 | Slack 4종 + 에러 · 일일 요약 · 헬스체크 · 런북 |

> 각 단계는 **이전 단계 산출물이 검증되어야** 다음으로 넘어간다. 상세는 `PLAN.md` §4 참조.

---

## 데이터베이스 & 데이터 수집 (Step 2)

### 1) 테이블 생성 — `sql/` SQL 실행 (Supabase SQL Editor)

데이터를 수집하기 전에 **반드시 테이블을 먼저 만든다.** Supabase 대시보드 → SQL Editor 에서 순서대로 실행:

```text
sql/001_market_data.sql   -- market_data 와이드 테이블(날짜별 1행) + updated_at 트리거
sql/002_market_data_rls.sql   -- ⚠ RLS 비활성화 (아래 경고 참고)
```

> 두 SQL 은 `scripts/gen_schema_sql.py` 가 `core/universe.py`·`core/market_spec.py` 로부터 자동 생성한다.
> 유니버스/지표를 바꾸면 `python scripts/gen_schema_sql.py` 로 재생성 후 다시 실행한다.

#### ⚠ RLS(Row Level Security) 주의 — 데이터 유실처럼 보이는 사고 예방

Supabase 는 신규 테이블에 RLS 가 켜질 수 있다. **RLS 가 켜져 있고 정책(policy)이 없으면, 데이터가 실제로 있어도 SELECT 가 0행을 반환**한다. 그러면 백필이 "데이터 없음"으로 오판해 **과거부터 전부 재수집하는 사고**가 난다. `market_data` 는 서버(서비스 키)만 접근하는 내부 테이블이므로 `002_market_data_rls.sql` 로 RLS 를 끈다. 실행 후 SQL Editor 에서 `SELECT count(*) FROM market_data;` 로 행이 보이는지 확인할 것.

### 2) 거시지표 API 키

- **ECOS_API_KEY** (한국은행): 한국 거시(기준금리·한국 CPI·수출/산업생산/경기선행). 미설정 시 해당 컬럼 NULL.
- **FRED_API_KEY** (미국): 미국 거시 14종(기준금리·2년물·장단기금리차·기대인플레·미국 CPI·실업률·소비자심리·M2·PCE·금융스트레스·모기지·GDP·가계부채·무역가중달러). 미설정 시 해당 컬럼 NULL. (미국 10년물/3개월은 yfinance ^TNX/^IRX 로 키 없이 수집)
- 지수/환율/원자재/글로벌지수/ETF/종목은 FinanceDataReader·yfinance 로 **키 없이** 수집된다.
- 거시/시장 지표는 총 ~48개(정답지 검토로 확장). 상세 목록·소스·코드는 `core/market_spec.py`.

### 3) 과거 백필

```bash
python scripts/backfill.py                       # 2010-01-01 ~ 어제, Supabase 저장(멱등)
python scripts/backfill.py --start 2015-01-01
python scripts/backfill.py --no-db               # 수집·검증만(저장 X) — 자격증명 없이 점검
```

- 멱등: 중단 후 재실행해도 안전(이미 채워진 컬럼은 덮어쓰지 않고, 비어있던 컬럼만 채움).
- 상장 시점이 다른 종목의 초기 구간 NaN 은 정상(그 컬럼만 비고, 행은 삭제하지 않음).
- 실행 끝에 **총 행 수 / 날짜 범위(min~max) / 종목별 종가 결측 비율**을 출력한다.

### 4) 증분 수집 (매일 자동)

초기 백필 이후에는 수동 구간 지정 없이 **증분 수집**으로 운영한다.

```text
POST /market/update?force=false   # 마지막 저장일+1 ~ 어제까지 자동 수집
```
- 마지막 행이 완성이면 다음 날부터, 미완성이면 그 날짜부터 재수집. 여러 번 돌려도 중복 없음(멱등).
- **장중(KST 평일 09:00~15:30) 당일 제외**(종가 미확정). `force=true` 면 당일 포함.
- **안전장치**: 증분 범위가 과도(45일 초과)하면 `force` 없이는 수집 안 함 — 초기적재/RLS 빈응답으로 인한 대량 재수집 사고 방지. 초기 적재는 `scripts/backfill.py` 사용.
- 앱 기동 시 자동 1회 실행(`COLLECT_ON_STARTUP=true`, 개발 reload 시 false 권장).

### 5) 수집/조회 API (FastAPI 컨트롤러)

```text
POST /market/collect   body: {"start":"2024-01-01","end":"2024-01-31"}   # 수동 구간 수집
POST /market/update?force=false                                          # 증분 수집
GET  /market/status    → {rows, min_date, max_date}
POST /flow/collect?lookback_days=5&ref_date=2024-06-05                   # 수급 점수 산출·저장
GET  /flow/scores      → 최근 기준일 종목별 flow_score 스냅샷
```

### 6) 외국인·기관 수급 점수 (`investor_flow`, 감성 대체)

`sql/004_investor_flow.sql` 실행 후 `/flow/collect` 또는 `flow_service.collect_and_store_flows()`.
- 종목별 외국인+기관 순매수(최근 N=5거래일 합) → 거래대금 대비 비율 → `flow_score`(-1~+1, 양수=호재성).
- (ticker, calculation_date) 복합키로 이력 보존. 거래정지·유동성 미달·기간 부족은 fail-close 로 제외.
- **데이터 소스(`FLOW_SOURCE`)**: 기본 **`naver`**(네이버 금융 외국인/기관 일별 순매매, **로그인 불필요** — 25/25 종목 실동작 확인). `pykrx`(KRX 로그인 필요·현 환경 차단), `auto`(naver→pykrx) 선택 가능.
- ⚠ stock-quant 권고(향후): 종목별 rolling z-score 정규화, 외국인/기관 분리, EWM 가중. `FLOW_SCORE_SCALE`(0.03)은 잠정값 — 강한 수급 구간에서 ±1 포화 관측, 백테스트 보정 필요.

### 데이터 무결성 핵심 (`core/market_spec.py`)

- **컬럼명=한글** (정답지 미국 시스템 패턴): 날짜=`"날짜"`, 종목 종가=한글 종목명(`"삼성전자"`=TARGET_COLUMNS), 거래량=`"삼성전자_거래량"`, 거시/지수=한글 지표명. ML 이 `data["삼성전자"]`처럼 직접 인덱싱하므로 컬럼명을 한글로 맞춤(타입 FLOAT8).
- **날짜 축=달력일(주말 포함, 정답지 방식).** 주말/공휴일은 직전 거래일 값으로 연속 ffill(거래량 포함). 연속 10일 초과 ffill만 '의심 갭'으로 경고. **거래일 행은 실제 값, 주말/공휴일 행은 ffill 연장값.**
- **결측은 ffill 만**(bfill/mean/0 금지). 거시는 발표일까지 ffill.
- **look-ahead 방지:** 해외 시장 컬럼(미국 지수·금리·달러·유가)은 ML 피처 빌더에서 **t-1 시프트** 필요. ECOS 월간 지표는 발표 시차로 **+1개월 보수 시프트**되어 저장됨.
- **VKOSPI 제외:** FDR/yfinance 미제공 + pykrx(KRX) 차단으로 수집 불가 → 컬럼에서 제거함. 보수 모드(CLAUDE.md §2.4)의 변동성 게이지는 **VIX 로 대용**.

---

## ML 학습 (Kaggle GPU, Step 4)

검증된 미국 시스템의 ML 로직(`kaggle_notebook/predict.py`, Transformer, LOOKBACK=90·14영업일 예측)을 **그대로** 쓰고, **종목/피처/테이블만 한국 데이터로 교체**했다(TARGET_COLUMNS=25종목, ECONOMIC_FEATURES=46개 거시/시장, 테이블=`market_data`).

### 1) 결과 테이블 생성 (Supabase SQL Editor)
```text
sql/005_stock_analysis_results.sql   -- 종목별 성능·추천(매 실행 갱신)
sql/006_predicted_stocks.sql         -- 종목별 예측값 vs 실제값(25종목×2 컬럼)
```

### 2) Kaggle 첫 셋업 (⚠ 최초 1회)
- **휴대폰 인증 필수**: Kaggle 계정에서 GPU/Internet 사용하려면 Phone Verification 필요(Settings → Phone).
- **노트북 설정**: `kernel-metadata.json` 에 `enable_gpu: true`, `enable_internet: true` 설정됨(코드 인터넷으로 Supabase 접근).
- **Secrets**: Kaggle UI 수동 실행 시 Add-ons → Secrets 에 `SUPABASE_URL_KOREA`/`SUPABASE_KEY_KOREA` 등록.
  (API push 로 돌릴 땐 `ml_trigger_service` 가 push 직전 `predict.ipynb` 첫 셀에 `os.environ` 으로 **자동 주입** — UserSecretsClient 가 push kernel 에서 안 되는 이슈 우회.)
- **자격증명**: `.env` 에 `KAGGLE_API_TOKEN`(단독) 또는 `KAGGLE_USERNAME`+`KAGGLE_KEY`. `KAGGLE_USERNAME` 은 실제 계정 username.

### 3) 트리거 / 인증 확인
```text
GET  /ml/auth-check   → {ok, message}        # Kaggle 인증 확인
POST /ml/trigger      → 노트북 push(실행 시작)  # predict.ipynb 재생성 후 push
GET  /ml/status       → {status}             # queued/running/complete/error
```
로컬에서 직접: `python -c "from services.ml_trigger_service import check_auth; print(check_auth())"`.
완료까지 대기: `ml_trigger_service.trigger_and_wait()` (push → complete 폴링).

> `predict.py` 가 커밋 원본이고, `predict.ipynb` 는 push 시 **시크릿 주입본으로 덮어써져** `.gitignore` 됨.
> ⚠ **bfill 주의**: 참고 코드의 `df.ffill().bfill()` 를 그대로 유지했는데, 미국 종목은 상장이력이 길어 무해했지만 **한국 신상장 종목(에코프로머티 2023, LG엔솔 2022 등)은 상장 전 구간을 미래값으로 backfill → look-ahead** 가 된다. 향후 학습 시작일을 전 종목 상장 이후로 자르거나 bfill 을 제거하는 보정 권장(CLAUDE.md §2.1).

---

## 안전 원칙 (요약)

- **모의투자가 기본값.** 실전 전환은 별도 의사결정 단계로만.
- **LLM 검토 실패 시 매수 차단(fail-close).** LLM은 거부권만, 신규 종목 추가 불가.
- **결측치는 ffill만 허용.** look-ahead bias 금지.
- **종목 유니버스 교체는 수동.** 자동 교체 코드 없음.
- 일일 잡은 **멱등** — 같은 날짜로 두 번 돌려도 안전. 주문은 중복 금지.

전체 규칙은 [`CLAUDE.md`](./CLAUDE.md) 참조.
