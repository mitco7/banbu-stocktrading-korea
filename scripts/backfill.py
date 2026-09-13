#!/usr/bin/env python
"""과거 시장데이터 백필 (수동 실행).

시작일부터 어제(KST)까지 collect_market_data 로 수집해 market_data 에 배치 저장한다.
멱등하므로 중단 후 재실행해도 안전하다(이미 채워진 컬럼은 건드리지 않음).

실행:
  python scripts/backfill.py                       # 2010-01-01 ~ 어제, Supabase 저장
  python scripts/backfill.py --start 2015-01-01
  python scripts/backfill.py --no-db               # 수집/검증만(저장 안 함) — 자격증명 없이 점검
  python scripts/backfill.py --start 2024-01-01 --end 2024-03-31 --no-db

상장 시점이 다른 종목의 초기 구간 NaN 은 정상(그 컬럼만 비고, 행 삭제하지 않음).
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime  # noqa: E402

from core.market_spec import STOCK_CODES, stock_close_col, stock_volume_col  # noqa: E402
from core.universe import CODE_TO_NAME  # noqa: E402
from db.repositories import market_data_repo  # noqa: E402
from services.collectors.stock_collector import fetch_stock  # noqa: E402
from services.market_data_service import collect_market_data, dataframe_to_records  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger("backfill")
KST = ZoneInfo("Asia/Seoul")
BATCH_ROWS = 1000


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="시장데이터 과거 백필")
    p.add_argument("--start", default="2010-01-01", help="시작일 YYYY-MM-DD")
    p.add_argument("--end", default=None, help="종료일 YYYY-MM-DD (기본: 어제 KST)")
    p.add_argument("--batch", type=int, default=BATCH_ROWS, help="저장 배치 행 수")
    p.add_argument("--no-db", action="store_true", help="Supabase 저장 생략(수집/검증만)")
    return p.parse_args()


def retry_failed_stocks(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """전부 NaN 인 종목 종가 컬럼(수집 실패 추정)을 1회 재시도."""
    failed = [c for c in STOCK_CODES if df[stock_close_col(c)].notna().sum() == 0]
    if not failed:
        return df
    logger.info("실패 종목 재시도: %s", ", ".join(failed))
    spine = df.index
    for code in failed:
        sdf = fetch_stock(code, start, end)
        if sdf is not None:
            df[stock_close_col(code)] = sdf["close"].reindex(spine).astype("float64")
            df[stock_volume_col(code)] = (
                pd.to_numeric(sdf["volume"], errors="coerce").reindex(spine).astype("float64")
            )
            logger.info("재시도 성공: %s", code)
    return df


def print_verification(df: pd.DataFrame) -> None:
    """적재 후 검증 출력: 총 행 수, 날짜 범위, 종목별 종가 결측 비율."""
    n = len(df)
    print("\n" + "=" * 60)
    print("[검증] 백필 결과")
    print(f"  총 행 수      : {n}")
    print(f"  날짜 범위     : {df.index.min().date()} ~ {df.index.max().date()}")
    print("-" * 60)
    print("  종목별 종가 결측 비율 (낮을수록 좋음; 상장 전 구간 포함):")
    print(f"  {'코드':<8}{'종목명':<14}{'채움행':>8}{'결측%':>8}")
    for code in STOCK_CODES:
        col = stock_close_col(code)
        filled = int(df[col].notna().sum())
        miss_pct = 100.0 * (n - filled) / n if n else 100.0
        print(f"  {code:<8}{CODE_TO_NAME[code]:<14}{filled:>8}{miss_pct:>7.1f}%")
    # 거시/시장 컬럼 수집 여부 요약.
    from core.market_spec import MACRO_MARKET_KEYS

    empty_macro = [k for k in MACRO_MARKET_KEYS if df[k].notna().sum() == 0]
    if empty_macro:
        print("-" * 60)
        print(f"  ⚠ 전부 결측인 거시/시장 컬럼({len(empty_macro)}): {', '.join(empty_macro)}")
        print("    (API 키 미설정 또는 본 환경 소스 차단 — 키 설정 후 재실행 시 채워짐)")
    print("=" * 60)


def batch_save(df: pd.DataFrame, batch: int) -> dict[str, int]:
    """1000행 단위로 멱등 저장(진행률 로그)."""
    records = dataframe_to_records(df)
    total = len(records)
    upserted = 0
    for i in range(0, total, batch):
        chunk = records[i : i + batch]
        res = market_data_repo.upsert_fill_empty(chunk)
        upserted += res["upserted"]
        logger.info("저장 진행 %d/%d (upserted=%d 누적)",
                    min(i + batch, total), total, upserted)
    return {"upserted": upserted}


def main() -> int:
    args = parse_args()
    end = args.end or (datetime.now(KST).date() - timedelta(days=1)).strftime("%Y-%m-%d")
    start = args.start
    logger.info("백필 시작: %s ~ %s (no_db=%s)", start, end, args.no_db)

    df = collect_market_data(start, end)
    df = retry_failed_stocks(df, start, end)

    print_verification(df)

    if args.no_db:
        print("\n[no-db] Supabase 저장을 생략했습니다(수집/검증만).")
        return 0

    try:
        agg = batch_save(df, args.batch)
    except RuntimeError as exc:
        logger.error("저장 실패(자격증명 등): %s", exc)
        print(f"\n[오류] Supabase 저장 실패: {exc}\n  → .env 의 SUPABASE_URL_KOREA/KEY 와 sql/ 테이블 생성을 확인하세요.")
        return 1

    print(f"\n[완료] 저장 결과: upserted={agg['upserted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
