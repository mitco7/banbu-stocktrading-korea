"""결측 처리 헬퍼 — ffill(직전 영업일 값 보간) 전용 (CLAUDE.md §2.1).

bfill / mean / median / 0 채움은 **금지**. 미래값으로 과거를 채우면 look-ahead bias.
날짜 축은 KRX 영업일 그리드여야 한다(주말·휴장일 행을 만들어 ffill하지 말 것).
일간 가격 컬럼은 연속 ffill 한도(영업일)를 두고, 초과 구간은 '데이터 갭'으로 분류해 알린다.
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
    """일간 가격 컬럼을 한도(limit) 영업일까지만 ffill.

    한도를 초과한 연속 결측 구간이 있으면 컬럼별 '갭 길이 최대값'을 반환하고,
    데이터 신뢰도 사후 감사를 위해 **컬럼별 ffill 로 채운 셀 수**도 반환한다.
    입력 index 는 영업일만이어야 한다(달력일+주말 행이면 한도 의미가 왜곡됨).

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
        # 첫 관측 이전의 선두 결측(상장 전 구간)은 갭이 아니다 — 제외.
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
    """영업일 그리드용 ffill — `ffill_limited` 래퍼(하위 호환).

    2026-10-27 이전: 달력일에서 한도 없이 ffill 하고 warn_run 초과만 경고했다.
    이후: warn_run 을 **한도**로 사용해 초과 구간은 NaN 유지 + gaps 반환.
    """
    out, gaps, _filled = ffill_limited(df, columns, limit=warn_run)
    return out, gaps


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
