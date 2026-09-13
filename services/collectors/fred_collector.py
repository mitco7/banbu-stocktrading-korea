"""FRED(미국 세인트루이스 연준) 거시지표 수집기.

API 키(FRED_API_KEY)가 없으면 None 반환(수집 생략, 컬럼 결측 유지).
일간 시리즈(DGS10, DGS2, DFF 등)는 그대로 일자 인덱스로 반환한다.
미국 데이터의 t-1 lag(KST 정렬) 는 ML 피처 빌더에서 적용한다(market_spec 참조).
"""

from __future__ import annotations

import pandas as pd
import requests

from core.config import settings
from utils.crypto import mask_secrets
from utils.logger import get_logger
from utils.retry import retry

logger = get_logger(__name__)

_BASE = "https://api.stlouisfed.org/fred/series/observations"
_TIMEOUT = 20


@retry(attempts=3, base_delay=2.0, backoff=2.0)
def _request(params: dict) -> dict:
    try:
        resp = requests.get(_BASE, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        # requests 예외의 URL 에 쿼리스트링(api_key)이 포함되므로 마스킹 후 재발생.
        raise RuntimeError(mask_secrets(str(exc), [settings.FRED_API_KEY])) from None


def fetch_fred_series(spec: dict, start: str, end: str) -> pd.Series | None:
    """FRED 단일 시리즈 수집.

    Args:
        spec: {"series_id": "DGS10"}
    Returns:
        pd.Series(index=date) 또는 None.
    """
    if not settings.FRED_API_KEY:
        logger.info("FRED_API_KEY 미설정 → %s 수집 생략", spec.get("series_id"))
        return None

    params = {
        "series_id": spec["series_id"],
        "api_key": settings.FRED_API_KEY,
        "file_type": "json",
        "observation_start": pd.Timestamp(start).strftime("%Y-%m-%d"),
        "observation_end": pd.Timestamp(end).strftime("%Y-%m-%d"),
    }
    try:
        data = _request(params)
    except Exception as exc:  # noqa: BLE001
        logger.warning("FRED 요청 실패(%s): %s", spec.get("series_id"), exc)
        return None

    obs = data.get("observations")
    if not obs:
        return None

    idx, vals = [], []
    for o in obs:
        v = o.get("value")
        if v in (None, "", "."):  # FRED 결측은 "."
            continue
        try:
            value = float(v)
        except ValueError:
            continue
        idx.append(pd.Timestamp(o["date"]))
        vals.append(value)

    if not idx:
        return None
    return pd.Series(vals, index=pd.DatetimeIndex(idx)).sort_index()
