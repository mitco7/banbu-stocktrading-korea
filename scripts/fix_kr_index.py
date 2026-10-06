#!/usr/bin/env python
"""KOSPI·KOSDAQ 고착 구간 교정 (수동 실행).

2026-09-17 부터 FDR 의 KRX 지수 응답이 9/17 자에서 멈춰(개별 종목은 정상) market_data 의
KOSPI/KOSDAQ 가 같은 값으로 ffill 고착됐다. 멱등 저장(upsert_fill_empty)은 '빈 칸만' 채우므로
이미 들어찬 가짜 값은 덮이지 않는다 → 이 스크립트가 네이버 실제 종가로 직접 덮어쓴다.

실행:
  python scripts/fix_kr_index.py --dry-run              # 비교만(쓰기 없음)
  python scripts/fix_kr_index.py                        # 2026-09-17 ~ 오늘 교정
  python scripts/fix_kr_index.py --start 2026-09-17 --end 2026-10-06

휴장일·주말은 직전 거래일 값으로 채운다(수집기의 달력일 ffill 규칙과 동일).
실행 전 현재 값을 JSON 으로 백업한다(기본 scripts/out/).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.market_spec import DATE_COL  # noqa: E402
from db.client import get_supabase  # noqa: E402
from services.collectors.price_sources import naver_index_close  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
TABLE = "market_data"
COLS = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ"}  # market_data 컬럼명 ← 네이버 symbol


def build_truth(start: str, end: str) -> pd.DataFrame:
    """네이버 종가를 달력일 그리드로 펴서 (날짜 × KOSPI/KOSDAQ) 반환."""
    grid = pd.date_range(start=start, end=end, freq="D")
    out = pd.DataFrame(index=grid)
    # 선두 행이 휴일이어도 채워지도록 2주 앞에서부터 받아온다.
    fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
    for col, symbol in COLS.items():
        s = naver_index_close(symbol, fetch_start, end)
        out[col] = s.reindex(out.index.union(s.index)).ffill().reindex(out.index)
    return out


def main() -> int:
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    # 기본 종료일은 '어제' — 장중 지수는 확정값이 아니다(CLAUDE.md §2.1 미확정 데이터 금지).
    default_end = (now - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    ap = argparse.ArgumentParser(description="KOSPI/KOSDAQ 고착 구간 교정")
    ap.add_argument("--start", default="2026-09-17")
    ap.add_argument("--end", default=default_end)
    ap.add_argument("--dry-run", action="store_true", help="비교만 출력하고 쓰지 않음")
    ap.add_argument("--allow-today", action="store_true",
                    help="당일 포함(장 마감·종가 확정 후에만 사용)")
    ap.add_argument("--backup-dir", default=str(Path(__file__).parent / "out"))
    args = ap.parse_args()

    if args.end >= today and not args.allow_today:
        print(f"당일({today})은 종가 확정 전이라 제외합니다. "
              f"장 마감 후 교정하려면 --allow-today 를 붙이세요.")
        args.end = (now - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    if args.end >= today and args.allow_today and now.hour * 60 + now.minute < 15 * 60 + 40:
        print("아직 장 마감(15:30) 전입니다. 종가 확정 후 다시 실행하세요.")
        return 1

    truth = build_truth(args.start, args.end)
    print(f"네이버 수집: {args.start} ~ {args.end} — {len(truth)}행 "
          f"(마지막 {truth.index[-1].date()} KOSPI {truth['KOSPI'].iloc[-1]:,.2f})")

    client = get_supabase()
    resp = (client.table(TABLE)
            .select(f"{DATE_COL},KOSPI,KOSDAQ")
            .gte(DATE_COL, args.start).lte(DATE_COL, args.end)
            .order(DATE_COL).execute())
    rows = resp.data or []
    if not rows:
        print("대상 구간에 행이 없습니다. 먼저 수집이 필요합니다.")
        return 1

    backup = Path(args.backup_dir)
    backup.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    bpath = backup / f"kr_index_backup_{stamp}.json"
    bpath.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"백업 저장: {bpath}")

    changes = []
    for row in rows:
        d = pd.Timestamp(row[DATE_COL])
        if d not in truth.index:
            continue
        patch = {}
        for col in COLS:
            new = truth.at[d, col]
            old = row.get(col)
            if pd.isna(new):
                continue
            if old is None or abs(float(old) - float(new)) >= 0.01:
                patch[col] = round(float(new), 2)
        if patch:
            changes.append((row[DATE_COL], row.get("KOSPI"), row.get("KOSDAQ"), patch))

    print(f"\n변경 대상 {len(changes)}행")
    for d, ok, oq, patch in changes[:40]:
        print(f"  {d}  KOSPI {ok} → {patch.get('KOSPI', '-')}   "
              f"KOSDAQ {oq} → {patch.get('KOSDAQ', '-')}")
    if len(changes) > 40:
        print(f"  … 외 {len(changes) - 40}행")

    if args.dry_run:
        print("\n--dry-run 이라 쓰지 않았습니다.")
        return 0
    if not changes:
        print("\n교정할 값이 없습니다.")
        return 0

    for d, _ok, _oq, patch in changes:
        client.table(TABLE).update(patch).eq(DATE_COL, d).execute()
    print(f"\n교정 완료: {len(changes)}행 업데이트")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
