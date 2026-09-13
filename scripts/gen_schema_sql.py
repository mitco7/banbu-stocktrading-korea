#!/usr/bin/env python
"""sql/ 의 market_data 스키마 SQL 을 core.market_spec + core.universe 로부터 생성한다.

스키마가 항상 유니버스/지표 스펙과 일치하도록 보장한다. 유니버스/지표가 바뀌면 재실행:
    python scripts/gen_schema_sql.py
생성물: sql/001_market_data.sql, sql/002_market_data_rls.sql, sql/003_market_data_upsert.sql

컬럼명은 한글(정답지 패턴)이므로 모든 식별자를 큰따옴표로 인용한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.market_spec import (  # noqa: E402
    DATE_COL,
    MACRO_MARKET_KEYS,
    MACRO_MARKET_SERIES,
    STOCK_CLOSE_COLS,
    STOCK_CODES,
    STOCK_VOLUME_COLS,
    stock_close_col,
    stock_volume_col,
)
from core.universe import CODE_TO_NAME, SELECTED_AT, TARGET_COLUMNS  # noqa: E402

SQL_DIR = PROJECT_ROOT / "sql"
TABLE = "market_data"
COL_TYPE = "FLOAT8"  # 정답지(economic_and_stock_data)와 동일

# upsert 대상 데이터 컬럼(날짜·감사컬럼 제외).
DATA_COLUMNS = MACRO_MARKET_KEYS + STOCK_CLOSE_COLS + STOCK_VOLUME_COLS


def q(name: str) -> str:
    """SQL 식별자 인용(한글/공백/특수문자 대비). 내부 큰따옴표는 이스케이프."""
    return '"' + name.replace('"', '""') + '"'


def build_create_sql() -> str:
    lines: list[str] = []
    lines.append(f"-- {TABLE}: 시장데이터 와이드 테이블 (날짜별 1행).")
    lines.append("-- 자동 생성: scripts/gen_schema_sql.py (직접 편집 금지).")
    lines.append(f"-- 유니버스 기준일(SELECTED_AT): {SELECTED_AT}")
    lines.append(f"-- 컬럼 단위: 모두 {COL_TYPE}. 결측은 NULL(상장 전 구간·미수집 지표 등).")
    lines.append("-- 컬럼명=한글(ML 의 TARGET_COLUMNS/ECONOMIC_FEATURES 와 직접 매칭).")
    lines.append("-- ⚠ 해외 시장 컬럼은 t-1 lag, 거시는 발표일 시프트가 ML 피처 단계에서 필요(core/market_spec.py).")
    lines.append("-- ⚠ 스키마 변경 후 재적용 시: 아래 CREATE 는 IF NOT EXISTS 라 '기존 테이블을 갱신하지 않는다'.")
    lines.append("--    컬럼이 추가/변경됐다면 먼저 기존 테이블을 비우고 드롭해야 한다(데이터 있으면 백업 후):")
    lines.append(f"--      DROP TABLE IF EXISTS {TABLE} CASCADE;")
    lines.append("")
    lines.append(f"CREATE TABLE IF NOT EXISTS {TABLE} (")

    col_lines: list[str] = []
    col_lines.append(f"    {q(DATE_COL)} DATE PRIMARY KEY")  # unique 보장

    col_lines.append("    -- ── 거시/시장 지표 ──")
    for s in MACRO_MARKET_SERIES:
        col_lines.append(f"    , {q(s.key)} {COL_TYPE}  -- [{s.category}/{s.freq}] {s.desc}")

    col_lines.append("    -- ── 25종목 종가(수정주가) ──")
    for code in STOCK_CODES:
        col_lines.append(f"    , {q(stock_close_col(code))} {COL_TYPE}  -- {code}")

    col_lines.append("    -- ── 25종목 거래량 ──")
    for code in STOCK_CODES:
        col_lines.append(f"    , {q(stock_volume_col(code))} {COL_TYPE}  -- {code}")

    col_lines.append("    -- ── 감사 컬럼 ──")
    col_lines.append("    , created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
    col_lines.append("    , updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")

    lines.append("\n".join(col_lines))
    lines.append(");")
    lines.append("")
    lines.append("-- updated_at 자동 갱신 트리거.")
    lines.append(
        "CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$\n"
        "BEGIN NEW.updated_at = NOW(); RETURN NEW; END;\n"
        "$$ LANGUAGE plpgsql;"
    )
    lines.append(
        f"DROP TRIGGER IF EXISTS trg_{TABLE}_updated_at ON {TABLE};\n"
        f"CREATE TRIGGER trg_{TABLE}_updated_at BEFORE UPDATE ON {TABLE}\n"
        f"    FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def build_rls_sql() -> str:
    return (
        f"-- {TABLE} RLS 비활성화.\n"
        "-- ⚠ 중요(사고 예방): Supabase 는 신규 테이블에 RLS 가 켜질 수 있고, 정책이 없으면\n"
        "--    service/anon 키로 SELECT 시 '데이터가 있어도 0행'이 반환된다.\n"
        "--    그러면 백필이 '데이터 없음'으로 오판해 과거부터 재수집하는 사고가 난다.\n"
        "--    이 테이블은 서버(서비스 키)만 접근하는 내부 데이터이므로 RLS 를 끈다.\n"
        f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY;\n"
    )


def build_upsert_fn_sql() -> str:
    """멱등 벌크 upsert 함수. NULL 컬럼만 COALESCE 로 채움(원자적, 한 문장).

    repo 에서 client.rpc("upsert_market_data_fill_empty", {"rows": [...]}) 로 호출.
    jsonb 배열을 받아 INSERT ... ON CONFLICT (날짜) DO UPDATE 한 번에 처리한다.
    """
    all_cols = [DATE_COL] + DATA_COLUMNS
    cols_quoted = ", ".join(q(c) for c in all_cols)
    set_lines = ",\n".join(
        f"            {q(c)} = COALESCE({TABLE}.{q(c)}, EXCLUDED.{q(c)})"
        for c in DATA_COLUMNS
    )
    return (
        f"-- {TABLE} 멱등 벌크 upsert 함수 (자동 생성: scripts/gen_schema_sql.py).\n"
        "-- '이미 있는 날짜는 NULL 컬럼만 채움, 없으면 insert' 를 컬럼별 COALESCE 로 원자 처리.\n"
        "-- 기존 비결측 값은 절대 덮어쓰지 않는다(멱등성 CLAUDE.md §2.7).\n"
        "CREATE OR REPLACE FUNCTION upsert_market_data_fill_empty(rows jsonb)\n"
        "RETURNS integer AS $$\n"
        "DECLARE affected integer;\n"
        "BEGIN\n"
        f"    INSERT INTO {TABLE} ({cols_quoted})\n"
        f"    SELECT {cols_quoted}\n"
        f"    FROM jsonb_populate_recordset(NULL::{TABLE}, rows)\n"
        f"    ON CONFLICT ({q(DATE_COL)}) DO UPDATE SET\n"
        f"{set_lines};\n"
        "    GET DIAGNOSTICS affected = ROW_COUNT;\n"
        "    RETURN affected;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n"
    )


def build_stock_analysis_sql() -> str:
    """stock_analysis_results: ML 종목별 성능·추천 결과(predict.py save_analysis_to_db 컬럼셋)."""
    return (
        "-- stock_analysis_results: ML 종목별 예측 성능·추천 결과.\n"
        "-- predict.py 의 save_analysis_to_db() 가 delete 후 insert(매 실행 갱신).\n"
        "CREATE TABLE IF NOT EXISTS stock_analysis_results (\n"
        "    id BIGSERIAL PRIMARY KEY,\n"
        '    "Stock" TEXT,\n'
        '    "MAE" FLOAT8,\n'
        '    "MSE" FLOAT8,\n'
        '    "RMSE" FLOAT8,\n'
        '    "MAPE (%)" FLOAT8,\n'
        '    "Accuracy (%)" FLOAT8,\n'
        '    "Last Actual Price" FLOAT8,\n'
        '    "Predicted Future Price" FLOAT8,\n'
        '    "Predicted Rise" BOOLEAN,\n'
        '    "Rise Probability (%)" FLOAT8,\n'
        '    "Recommendation" TEXT,\n'
        '    "Analysis" TEXT,\n'
        "    created_at TIMESTAMPTZ DEFAULT NOW()\n"
        ");\n\n"
        "ALTER TABLE stock_analysis_results DISABLE ROW LEVEL SECURITY;\n"
    )


def build_predicted_stocks_sql() -> str:
    """predicted_stocks: 종목별 예측값 vs 실제값(날짜별). 컬럼은 TARGET_COLUMNS 기준 자동 생성."""
    lines = [
        "-- predicted_stocks: ML 종목별 예측값 vs 실제값(날짜별).",
        "-- predict.py 의 save_predictions_to_db() 가 delete 후 insert(매 실행 갱신).",
        f"-- 컬럼은 25종목(TARGET_COLUMNS) 기준 자동 생성. 유니버스 기준일: {SELECTED_AT}",
        "CREATE TABLE IF NOT EXISTS predicted_stocks (",
        "    id BIGSERIAL PRIMARY KEY",
        f'    , {q(DATE_COL)} DATE NOT NULL',
    ]
    for name in TARGET_COLUMNS:
        lines.append(f"    , {q(name + '_Predicted')} FLOAT8")
        lines.append(f"    , {q(name + '_Actual')} FLOAT8")
    lines.append(");")
    lines.append("")
    lines.append(f"CREATE INDEX IF NOT EXISTS idx_predicted_stocks_date ON predicted_stocks ({q(DATE_COL)});")
    lines.append("ALTER TABLE predicted_stocks DISABLE ROW LEVEL SECURITY;")
    return "\n".join(lines) + "\n"


def main() -> int:
    SQL_DIR.mkdir(exist_ok=True)
    outputs = {
        "001_market_data.sql": build_create_sql(),
        "002_market_data_rls.sql": build_rls_sql(),
        "003_market_data_upsert.sql": build_upsert_fn_sql(),
        "005_stock_analysis_results.sql": build_stock_analysis_sql(),
        "006_predicted_stocks.sql": build_predicted_stocks_sql(),
    }
    for name, content in outputs.items():
        (SQL_DIR / name).write_text(content, encoding="utf-8")
    n_cols = len(MACRO_MARKET_SERIES) + 2 * len(STOCK_CODES) + 3
    print(f"생성 완료: 001({n_cols}컬럼)·002·003·005·006 "
          f"(predicted_stocks {2 * len(TARGET_COLUMNS)}개 종목컬럼)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
