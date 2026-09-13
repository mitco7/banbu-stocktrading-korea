"""결측 처리 헬퍼 — ffill(전일치 보간) 전용 (CLAUDE.md §2.1).

bfill / mean / median / 0 채움은 **금지**. 미래값으로 과거를 채우면 look-ahead bias.
일간 가격 컬럼은 연속 ffill 한도를 두고, 초과 구간은 '데이터 갭'으로 분류해 알린다.
"""

from __future__ import annotations

import pandas as pd


def ffill_only(df: pd.DataFrame, columns: list[str] | None = None) -> pd.DataFrame:
    """지정 컬럼을 forward-fill 만 적용(한도 없음). 거시(월간/비정기) 컬럼용.

    발표일까지 같은 값을 유지하는 것이 정상이므로 한도를 두지 않는다.
    """
    out = df.copy()
    cols = columns if columns is not None else list(out.columns)
    cols = [c for c in cols if c in out.columns]
    out[cols] = out[cols].ffill()
    return out


def ffill_limited(
    df: pd.DataFrame, columns: list[str], limit: int
) -> tuple[pd.DataFrame, dict[str, int], dict[str, int]]:
    """일간 가격 컬럼을 한도(limit) 거래일까지만 ffill.

    한도를 초과한 연속 결측 구간이 있으면 컬럼별 '갭 길이 최대값'을 반환하고,
    데이터 신뢰도 사후 감사를 위해 **컬럼별 ffill 로 채운 셀 수**도 반환한다.

    Returns:
        (보간된 DataFrame,
         {컬럼: 한도초과 연속결측 최대길이},
         {컬럼: ffill 로 채운 셀 수})
    """
    out = df.copy()
    gaps: dict[str, int] = {}
    filled_counts: dict[str, int] = {}
    cols = [c for c in columns if c in out.columns]
    for c in cols:
        col = out[c]
        # 한도 내 ffill 결과.
        filled = col.ffill(limit=limit)
        # ffill 로 새로 채워진 셀 수(원본 NaN → 보간 후 값).
        n_filled = int((col.isna() & filled.notna()).sum())
        if n_filled:
            filled_counts[c] = n_filled
        # 원본이 결측이고 한도 ffill 후에도 여전히 결측인 위치 = 한도 초과 갭.
        over_gap = col.isna() & filled.isna()
        # 첫 관측 이전의 선두 결측(상장 전 구간·구간 첫 휴장일 등)은 갭이 아니다 — 제외.
        first_valid = col.first_valid_index()
        if first_valid is not None:
            over_gap.loc[:first_valid] = False
            if over_gap.any():
                gaps[c] = int(_max_consecutive(over_gap))
        out[c] = filled
    return out, gaps, filled_counts


def ffill_grid(
    df: pd.DataFrame, columns: list[str], warn_run: int
) -> tuple[pd.DataFrame, dict[str, int]]:
    """달력일 그리드용 연속 ffill (주말/공휴일 연장).

    한도 없이 forward-fill 하되(연속 그리드 유지), 첫 관측 이후 **연속 ffill 길이가
    warn_run(일)을 초과**하면 '의심 데이터 갭'으로 보고한다. 정상 연휴(설/추석, 5~6일)는
    통과하고, 소스 장애 같은 비정상 장기 결측만 경고하기 위함이다(CLAUDE.md §2.1 취지).
    선두 결측(첫 관측 이전·상장 전 구간)은 ffill 대상이 아니며 갭으로 세지 않는다.

    Returns:
        (보간된 DataFrame, {컬럼: 한도초과 연속 ffill 최대길이})
    """
    out = df.copy()
    suspicious: dict[str, int] = {}
    cols = [c for c in columns if c in out.columns]
    for c in cols:
        col = out[c]
        filled = col.ffill()
        first_valid = col.first_valid_index()
        if first_valid is not None:
            was_filled = col.isna() & filled.notna()
            was_filled.loc[:first_valid] = False
            run = _max_consecutive(was_filled)
            if run > warn_run:
                suspicious[c] = int(run)
        out[c] = filled
    return out, suspicious


def _max_consecutive(mask: pd.Series) -> int:
    """불리언 시리즈에서 True 가 연속된 최대 길이."""
    longest = current = 0
    for v in mask:
        if v:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest
