#!/usr/bin/env python
"""수동 실행용 종목 유니버스 선정/검증 스크립트.

핵심 원칙 (CLAUDE.md §2.3 거버넌스):
  - '확정 유니버스'는 사람이 정의한다. 이 스크립트에서는 FIXED_UNIVERSE(아래 25종목)가
    유일한 권위이며, --write 시 이것만 core/universe.py 로 기록된다.
  - 시총·거래대금 종합순위 '자동 상위 N'은 **참고용**으로만 출력한다(수동 리밸런싱 의사결정 보조).
    자동 교체는 절대 하지 않는다.

데이터 소스:
  - 기본은 pykrx(시총 + 최근 N거래일 일평균 거래대금). pykrx가 KRX에 접속하지 못하면
    FinanceDataReader(FDR)로 자동 폴백한다(시총 + 기준일 1일치 거래대금 proxy, ARCHITECTURE 보조 소스).
  - --source 로 강제 지정 가능: auto(기본) / pykrx / fdr

실행:
  python scripts/select_universe.py                 # dry-run: 표만 출력, 파일 미기록
  python scripts/select_universe.py --write          # core/universe.py 생성/덮어쓰기
  python scripts/select_universe.py --days 20 --top 25 --rank-method rank-sum

완료 후:
  python -c "from core.universe import KR_UNIVERSE, validate_universe; validate_universe()"
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

# 프로젝트 루트를 import 경로에 추가(스크립트 직접 실행 시 core/ 접근용).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

KST = ZoneInfo("Asia/Seoul")
UNIVERSE_PATH = PROJECT_ROOT / "core" / "universe.py"

# ──────────────────────────────────────────────────────────────────────────
#  확정 유니버스 (사람이 정의 — 유일한 권위). (종목코드, 종목명, 시장)
#  코드는 6자리 문자열(앞자리 0 보존). FDR 마스터로 코드↔이름 교차검증 완료.
#  ※ 종목 교체는 운영자가 이 리스트를 직접 수정한 뒤 재실행하는 방식으로만 한다.
# ──────────────────────────────────────────────────────────────────────────
FIXED_UNIVERSE: list[tuple[str, str, str]] = [
    ("005930", "삼성전자", "KOSPI"),
    ("000660", "SK하이닉스", "KOSPI"),
    ("373220", "LG에너지솔루션", "KOSPI"),
    ("207940", "삼성바이오로직스", "KOSPI"),
    ("005380", "현대차", "KOSPI"),
    ("000270", "기아", "KOSPI"),
    ("012330", "현대모비스", "KOSPI"),
    ("028260", "삼성물산", "KOSPI"),
    ("035420", "NAVER", "KOSPI"),
    ("005490", "포스코홀딩스", "KOSPI"),
    ("051910", "LG화학", "KOSPI"),
    ("068270", "셀트리온", "KOSPI"),
    ("012450", "한화에어로스페이스", "KOSPI"),
    ("329180", "HD현대중공업", "KOSPI"),
    ("034020", "두산에너빌리티", "KOSPI"),
    ("009150", "삼성전기", "KOSPI"),
    ("035720", "카카오", "KOSPI"),
    ("259960", "크래프톤", "KOSPI"),
    ("352820", "하이브", "KOSPI"),
    ("017670", "SK텔레콤", "KOSPI"),
    ("450080", "에코프로머티", "KOSPI"),
    ("047810", "한국항공우주", "KOSPI"),
    ("066570", "LG전자", "KOSPI"),
    ("096770", "SK이노베이션", "KOSPI"),
    ("204320", "HL만도", "KOSPI"),
]

VALID_MARKETS = {"KOSPI", "KOSDAQ"}

# 우선주 패턴: "...우", "...우B", "...2우B" 등 (보통주 코드는 통상 끝자리 0).
_PREFERRED_RE = re.compile(r"\d*우[A-Z]?$")


# ──────────────────────────────────────────────────────────────────────────
#  제외 분류 (우선주/스팩/ETF/ETN/리츠/관리·정리매매) — "가능한 범위에서" best-effort
# ──────────────────────────────────────────────────────────────────────────
def classify_exclusion(code: str, name: str, market: str, dept: str | None) -> str | None:
    """제외 사유 문자열을 반환. 제외 대상이 아니면 None."""
    if market not in VALID_MARKETS:
        return f"시장({market})"  # KONEX 등 범위 밖
    if "스팩" in name or "기업인수목적" in name:
        return "SPAC"
    if "리츠" in name:
        return "REIT"
    if "ETN" in name:
        return "ETN"
    if "ETF" in name or "KODEX" in name or "TIGER" in name or "KBSTAR" in name:
        return "ETF"
    if _PREFERRED_RE.search(name):
        return "우선주"
    if dept:
        d = str(dept)
        if "관리" in d:
            return "관리종목"
        if "정리" in d:
            return "정리매매"
    return None


# ──────────────────────────────────────────────────────────────────────────
#  시장 스냅샷 제공자 — 두 백엔드 모두 동일 스키마 DataFrame 반환:
#    index = code(6자리 str), columns = [name, market, marcap, trade_value, dept]
#    marcap/trade_value 단위 = KRW
# ──────────────────────────────────────────────────────────────────────────
def _snapshot_fdr(days: int) -> tuple[pd.DataFrame, str, str]:
    """FinanceDataReader 폴백. 거래대금은 기준일 1일치(proxy)."""
    import FinanceDataReader as fdr

    listing = fdr.StockListing("KRX")
    df = pd.DataFrame(
        {
            "code": listing["Code"].astype(str).str.zfill(6),
            "name": listing["Name"].astype(str),
            "market": listing["Market"].astype(str),
            "marcap": pd.to_numeric(listing.get("Marcap"), errors="coerce"),
            "trade_value": pd.to_numeric(listing.get("Amount"), errors="coerce"),
            "dept": listing.get("Dept"),
        }
    ).set_index("code")
    asof = datetime.now(KST).strftime("%Y-%m-%d")
    note = f"FDR 스냅샷(기준일 1일치 거래대금 proxy; N={days}일 평균은 pykrx 사용 시 적용)"
    return df, asof, note


def _snapshot_pykrx(date_str: str | None, days: int) -> tuple[pd.DataFrame, str, str]:
    """pykrx 기본 경로. 시총 + 최근 N거래일 일평균 거래대금."""
    from pykrx import stock

    bday = (
        stock.get_nearest_business_day_in_a_week(date_str)
        if date_str
        else stock.get_nearest_business_day_in_a_week()
    )

    # 최근 N거래일: 유동성 높은 종목(삼성전자)의 일자 인덱스로 영업일 도출.
    lookback_start = (
        datetime.strptime(bday, "%Y%m%d") - pd.Timedelta(days=days * 2 + 10)
    ).strftime("%Y%m%d")
    bdays = list(
        stock.get_market_ohlcv_by_date(lookback_start, bday, "005930").index
    )
    bdays = [d.strftime("%Y%m%d") for d in bdays][-days:]

    rows: dict[str, dict] = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        cap = stock.get_market_cap_by_ticker(bday, market=mkt)
        for ticker, r in cap.iterrows():
            code = str(ticker).zfill(6)
            rows[code] = {
                "name": stock.get_market_ticker_name(ticker),
                "market": mkt,
                "marcap": float(r.get("시가총액", float("nan"))),
                "_tv_sum": 0.0,
                "_tv_cnt": 0,
                "dept": None,
            }

    # N거래일 거래대금 누적 → 평균.
    for d in bdays:
        for mkt in ("KOSPI", "KOSDAQ"):
            try:
                day_df = stock.get_market_ohlcv_by_ticker(d, market=mkt)
            except Exception:
                continue
            for ticker, r in day_df.iterrows():
                code = str(ticker).zfill(6)
                if code in rows:
                    val = r.get("거래대금")
                    if pd.notna(val):
                        rows[code]["_tv_sum"] += float(val)
                        rows[code]["_tv_cnt"] += 1

    recs = []
    for code, v in rows.items():
        tv = v["_tv_sum"] / v["_tv_cnt"] if v["_tv_cnt"] else float("nan")
        recs.append(
            {
                "code": code,
                "name": v["name"],
                "market": v["market"],
                "marcap": v["marcap"],
                "trade_value": tv,
                "dept": v["dept"],
            }
        )
    df = pd.DataFrame(recs).set_index("code")
    asof = datetime.strptime(bday, "%Y%m%d").strftime("%Y-%m-%d")
    note = f"pykrx 스냅샷(시총 + 최근 {days}거래일 일평균 거래대금)"
    return df, asof, note


def load_snapshot(
    source: str, date_str: str | None, days: int
) -> tuple[pd.DataFrame, str, str]:
    """source(auto/pykrx/fdr)에 따라 스냅샷 로드. auto는 pykrx 실패 시 FDR 폴백."""
    if source == "fdr":
        return _snapshot_fdr(days)
    if source == "pykrx":
        return _snapshot_pykrx(date_str, days)
    # auto
    try:
        df, asof, note = _snapshot_pykrx(date_str, days)
        if df.empty or df["marcap"].notna().sum() == 0:
            raise RuntimeError("pykrx 응답이 비어 있음")
        return df, asof, note
    except Exception as exc:  # noqa: BLE001 — 폴백 목적의 광범위 캐치
        print(f"[경고] pykrx 사용 불가 → FDR 폴백: {type(exc).__name__}: {exc}", file=sys.stderr)
        return _snapshot_fdr(days)


# ──────────────────────────────────────────────────────────────────────────
#  자동 상위 N 산출 (참고용)
# ──────────────────────────────────────────────────────────────────────────
def compute_reference_top(
    df: pd.DataFrame, top: int, rank_method: str
) -> pd.DataFrame:
    """제외 필터 적용 후 시총·거래대금 종합순위 상위 top 종목 반환."""
    work = df.copy()
    work["exclude_reason"] = [
        classify_exclusion(code, row["name"], row["market"], row["dept"])
        for code, row in work.iterrows()
    ]
    eligible = work[work["exclude_reason"].isna()].copy()
    eligible = eligible.dropna(subset=["marcap"])

    if rank_method == "zscore-sum":
        def _z(s: pd.Series) -> pd.Series:
            std = s.std(ddof=0)
            return (s - s.mean()) / std if std and std > 0 else s * 0.0

        eligible["score"] = _z(eligible["marcap"]) + _z(
            eligible["trade_value"].fillna(eligible["trade_value"].min())
        )
        eligible = eligible.sort_values("score", ascending=False)
    else:  # rank-sum (기본): 시총순위 + 거래대금순위 합이 작을수록 상위
        cap_rank = eligible["marcap"].rank(ascending=False, method="min")
        tv_rank = (
            eligible["trade_value"].rank(ascending=False, method="min")
        ).fillna(len(eligible))
        eligible["rank_sum"] = cap_rank + tv_rank
        eligible = eligible.sort_values("rank_sum", ascending=True)

    return eligible.head(top)


# ──────────────────────────────────────────────────────────────────────────
#  출력 헬퍼
# ──────────────────────────────────────────────────────────────────────────
def _fmt_eok(value: float) -> str:
    """KRW → 억원 표기."""
    if value is None or pd.isna(value):
        return "N/A"
    return f"{value / 1e8:,.0f}억"


def enrich_fixed(df: pd.DataFrame) -> list[dict]:
    """확정 유니버스 25종목에 스냅샷의 시총/거래대금 결합."""
    out = []
    for code, name, market in FIXED_UNIVERSE:
        row = df.loc[code] if code in df.index else None
        out.append(
            {
                "code": code,
                "name": name,
                "market": market,
                "marcap": None if row is None else row.get("marcap"),
                "trade_value": None if row is None else row.get("trade_value"),
                "src_name": None if row is None else row.get("name"),
            }
        )
    return out


def print_fixed_table(enriched: list[dict]) -> None:
    print("\n[확정 유니버스] — core/universe.py 에 기록될 25종목 (사람이 정의, 권위)")
    print(f"{'#':>2} {'코드':<7} {'종목명':<14} {'시장':<6} {'시가총액':>14} {'거래대금':>14}")
    print("-" * 64)
    for i, e in enumerate(enriched, 1):
        print(
            f"{i:>2} {e['code']:<7} {e['name']:<14} {e['market']:<6} "
            f"{_fmt_eok(e['marcap']):>14} {_fmt_eok(e['trade_value']):>14}"
        )
    missing = [e['code'] for e in enriched if e['marcap'] is None]
    if missing:
        print(f"  ⚠ 스냅샷에서 시총을 찾지 못한 코드(메모 N/A): {', '.join(missing)}")


def print_reference_table(ref: pd.DataFrame, fixed_codes: set[str]) -> None:
    print("\n[참고: 시총+거래대금 자동 상위] — 의사결정 보조용일 뿐, 교체 아님")
    print(f"{'#':>2} {'코드':<7} {'종목명':<16} {'시장':<6} {'시가총액':>14} {'in확정?'}")
    print("-" * 64)
    for i, (code, row) in enumerate(ref.iterrows(), 1):
        mark = "○" if code in fixed_codes else "—"
        print(
            f"{i:>2} {code:<7} {str(row['name'])[:16]:<16} {row['market']:<6} "
            f"{_fmt_eok(row['marcap']):>14} {mark:>6}"
        )


def print_diff(ref: pd.DataFrame, enriched: list[dict]) -> None:
    fixed_codes = {e["code"] for e in enriched}
    ref_codes = set(ref.index)
    only_fixed = [
        (e["code"], e["name"]) for e in enriched if e["code"] not in ref_codes
    ]
    only_ref = [
        (code, str(row["name"])) for code, row in ref.iterrows() if code not in fixed_codes
    ]
    print("\n[차이 분석] 확정 vs 자동 상위")
    print(f"  · 확정에만 있음(자동 순위권 밖): {len(only_fixed)}종목")
    for code, name in only_fixed:
        print(f"      {code} {name}")
    print(f"  · 자동 상위에만 있음(확정 미포함): {len(only_ref)}종목")
    for code, name in only_ref:
        print(f"      {code} {name}")


# ──────────────────────────────────────────────────────────────────────────
#  core/universe.py 생성
# ──────────────────────────────────────────────────────────────────────────
def render_universe_module(
    enriched: list[dict], selected_at: str, source_note: str, rank_method: str, days: int, top: int
) -> str:
    gen_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %Z")

    memo_lines = []
    for i, e in enumerate(enriched, 1):
        memo_lines.append(
            f"#   {i:>2}. {e['code']} {e['name']:<14} {e['market']:<6} "
            f"시총={_fmt_eok(e['marcap'])}  거래대금={_fmt_eok(e['trade_value'])}"
        )
    memo_block = "\n".join(memo_lines)

    universe_repr = ",\n".join(
        f'    ("{e["code"]}", "{e["name"]}", "{e["market"]}")' for e in enriched
    )

    return f'''"""한국 주식 매매 유니버스 (고정 — 자동 생성 파일).

이 파일은 scripts/select_universe.py 가 생성/덮어쓴다. 직접 편집하지 말 것.
종목 교체(수동 리밸런싱)는 select_universe.py 의 FIXED_UNIVERSE 를 수정한 뒤
`python scripts/select_universe.py --write` 로 재생성한다(CLAUDE.md §2.3 수동 거버넌스).

선정 기준일(SELECTED_AT): {selected_at}
생성 시각              : {gen_at}
데이터 소스            : {source_note}
선정 방식              : 확정 유니버스는 사람이 정의(권위). 시총+거래대금 자동 상위{top}는
                         참고용으로만 산출(rank_method={rank_method}, N={days}거래일). 자동 교체 없음.

종목별 시총·거래대금 메모(선정 기준일 기준, 참고용):
{memo_block}
"""

from __future__ import annotations

# (종목코드, 종목명, 시장) — 코드는 6자리 문자열(앞자리 0 보존).
KR_UNIVERSE: list[tuple[str, str, str]] = [
{universe_repr},
]

# 선정 기준일(수동 리밸런싱 이력 추적용).
SELECTED_AT: str = "{selected_at}"

# 조회용 매핑.
CODE_TO_NAME: dict[str, str] = {{code: name for code, name, _ in KR_UNIVERSE}}
NAME_TO_CODE: dict[str, str] = {{name: code for code, name, _ in KR_UNIVERSE}}
CODE_TO_MARKET: dict[str, str] = {{code: market for code, _, market in KR_UNIVERSE}}

# ML/분석에서 사용할 종목 컬럼명(종목명 기준 통일). 데이터 조인은 코드 기준 권장.
TARGET_COLUMNS: list[str] = [name for _, name, _ in KR_UNIVERSE]

_VALID_MARKETS = {{"KOSPI", "KOSDAQ"}}


def validate_universe() -> None:
    """유니버스 무결성 검사. 실패 시 ValueError 발생.

    - 종목코드가 6자리 숫자 문자열인지
    - 코드 중복이 없는지
    - 시장 값이 KOSPI/KOSDAQ 중 하나인지
    - 파생 매핑/컬럼이 KR_UNIVERSE 와 일관적인지
    """
    seen: set[str] = set()
    for code, name, market in KR_UNIVERSE:
        if not (isinstance(code, str) and len(code) == 6 and code.isdigit()):
            raise ValueError(f"종목코드는 6자리 숫자 문자열이어야 합니다: {{code!r}} ({{name}})")
        if market not in _VALID_MARKETS:
            raise ValueError(f"시장 값이 올바르지 않습니다: {{market!r}} ({{code}} {{name}})")
        if code in seen:
            raise ValueError(f"중복 종목코드: {{code}} ({{name}})")
        seen.add(code)

    if len(KR_UNIVERSE) != len(TARGET_COLUMNS):
        raise ValueError("KR_UNIVERSE 와 TARGET_COLUMNS 길이가 다릅니다.")
    if set(CODE_TO_NAME) != seen or set(CODE_TO_MARKET) != seen:
        raise ValueError("CODE_TO_NAME / CODE_TO_MARKET 가 KR_UNIVERSE 와 불일치합니다.")
    if len(NAME_TO_CODE) != len(KR_UNIVERSE):
        raise ValueError("종목명 중복으로 NAME_TO_CODE 가 손실되었습니다.")


if __name__ == "__main__":
    validate_universe()
    print(f"OK — {{len(KR_UNIVERSE)}}종목, SELECTED_AT={{SELECTED_AT}}")
'''


def write_universe_module(content: str) -> None:
    UNIVERSE_PATH.write_text(content, encoding="utf-8")
    print(f"\n[기록] core/universe.py 생성/덮어쓰기 완료 → {UNIVERSE_PATH}")


# ──────────────────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="종목 유니버스 선정/검증 (수동 실행)")
    p.add_argument("--date", default=None, help="기준일 YYYYMMDD (기본: 최근 영업일)")
    p.add_argument("--days", type=int, default=20, help="거래대금 평균 거래일 수 (기본 20)")
    p.add_argument("--top", type=int, default=25, help="참고용 자동 상위 N (기본 25)")
    p.add_argument(
        "--rank-method",
        choices=["rank-sum", "zscore-sum"],
        default="rank-sum",
        help="자동 순위 산출 방식 (기본 rank-sum)",
    )
    p.add_argument(
        "--source",
        choices=["auto", "pykrx", "fdr"],
        default="auto",
        help="데이터 소스 (기본 auto: pykrx 실패 시 FDR 폴백)",
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="core/universe.py 를 실제로 생성/덮어쓰기 (없으면 dry-run 미리보기)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print(f"기준일 입력={args.date or '최근 영업일'}, 거래대금 N={args.days}일, "
          f"자동상위={args.top}, 방식={args.rank_method}, 소스={args.source}")

    df, selected_at, source_note = load_snapshot(args.source, args.date, args.days)
    print(f"스냅샷 로드 완료: {len(df)}종목 | 기준일={selected_at} | {source_note}")

    enriched = enrich_fixed(df)
    ref = compute_reference_top(df, args.top, args.rank_method)
    fixed_codes = {e["code"] for e in enriched}

    print_fixed_table(enriched)
    print_reference_table(ref, fixed_codes)
    print_diff(ref, enriched)

    content = render_universe_module(
        enriched, selected_at, source_note, args.rank_method, args.days, args.top
    )

    if args.write:
        write_universe_module(content)
        # 생성 직후 검증.
        import importlib
        import core.universe as u  # noqa: WPS433

        importlib.reload(u)
        u.validate_universe()
        print(f"[검증] validate_universe() 통과 — {len(u.KR_UNIVERSE)}종목, "
              f"SELECTED_AT={u.SELECTED_AT}")
    else:
        print("\n[dry-run] 파일을 쓰지 않았습니다. 확정하려면 --write 를 붙여 다시 실행하세요.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
