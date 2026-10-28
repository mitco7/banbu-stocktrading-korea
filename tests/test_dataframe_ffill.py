"""영업일 ffill 한도 — 주말 행을 만들지 않는 전제(CLAUDE.md §2.1)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from utils.dataframe import ffill_limited


def test_ffill_limited_fills_within_limit_only():
    idx = pd.to_datetime(["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-28", "2026-09-29"])
    s = pd.Series([10.0, np.nan, np.nan, np.nan, 20.0], index=idx)
    df = pd.DataFrame({"x": s})
    out, gaps, filled = ffill_limited(df, ["x"], limit=2)
    # 22·23은 한도 2로 채워짐, 28은 한도 초과 → NaN, gaps 기록
    assert out["x"].iloc[1] == 10.0
    assert out["x"].iloc[2] == 10.0
    assert pd.isna(out["x"].iloc[3])
    assert gaps.get("x", 0) >= 1
    assert filled.get("x", 0) == 2


def test_ffill_limited_does_not_bfill_leading():
    idx = pd.to_datetime(["2026-09-28", "2026-09-29"])
    df = pd.DataFrame({"x": [np.nan, 5.0]}, index=idx)
    out, gaps, filled = ffill_limited(df, ["x"], limit=3)
    assert pd.isna(out["x"].iloc[0])
    assert out["x"].iloc[1] == 5.0
    assert filled == {}
