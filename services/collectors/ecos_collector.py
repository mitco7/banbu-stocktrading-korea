"""한국은행 ECOS Open API 거시지표 수집기.

API 키(ECOS_API_KEY)가 없으면 None 을 반환한다(수집 생략, 해당 컬럼 결측 유지).
월간 지표는 **발표 시차(publication lag)** 를 보수적으로 +1개월 시프트해
"실제로 알 수 있게 된 날짜"로 노출한다(look-ahead 방지, CLAUDE.md §2.1).
정확한 발표일 매핑이 확보되면 이 근사를 교체할 것.
"""

from __future__ import annotations

import pandas as pd
import requests

from core.config import settings
from utils.crypto import mask_secrets
from utils.logger import get_logger
from utils.retry import retry

logger = get_logger(__name__)

_BASE = "https://ecos.bok.or.kr/api/StatisticSearch"
_TIMEOUT = 20
_MAX_ROWS = 100000


def _fmt_date(d: str, cycle: str) -> str:
    """cycle 에 맞춘 ECOS 날짜 포맷. (D=YYYYMMDD, M=YYYYMM, Q=YYYYQn, A=YYYY)"""
    ts = pd.Timestamp(d)
    if cycle == "D":
        return ts.strftime("%Y%m%d")
    if cycle == "M":
        return ts.strftime("%Y%m")
    if cycle == "Q":
        return f"{ts.year}Q{(ts.month - 1) // 3 + 1}"
    if cycle == "A":
        return ts.strftime("%Y")
    return ts.strftime("%Y%m%d")


def _parse_time(t: str, cycle: str) -> pd.Timestamp:
    if cycle == "D":
        return pd.Timestamp(t)
    if cycle == "M":
        return pd.Timestamp(f"{t[:4]}-{t[4:6]}-01")
    if cycle == "Q":
        q = int(t[-1])
        return pd.Timestamp(f"{t[:4]}-{(q - 1) * 3 + 1:02d}-01")
    if cycle == "A":
        return pd.Timestamp(f"{t}-01-01")
    return pd.Timestamp(t)


@retry(attempts=3, base_delay=2.0, backoff=2.0)
def _request(url: str) -> dict:
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        # 예외 메시지(및 체인)에 URL 경로의 API 키가 들어가므로 마스킹 후 재발생.
        raise RuntimeError(mask_secrets(str(exc), [settings.ECOS_API_KEY])) from None


def fetch_ecos_series(spec: dict, start: str, end: str) -> pd.Series | None:
    """ECOS 단일 시계열 수집.

    Args:
        spec: {"stat_code", "cycle", "item_code1"(optional), "item_code2"(optional)}
    Returns:
        pd.Series(index=availability date) 또는 None(키 없음/실패/빈 응답).
    """
    if not settings.ECOS_API_KEY:
        logger.info("ECOS_API_KEY 미설정 → %s 수집 생략", spec.get("stat_code"))
        return None

    stat_code = spec["stat_code"]
    cycle = spec.get("cycle", "M")
    # 조회 구간 widen·발표시차 시프트는 service(_apply_publication_shift)에서 일원 처리.
    s = _fmt_date(start, cycle)
    e = _fmt_date(end, cycle)

    parts = [
        _BASE, settings.ECOS_API_KEY, "json", "kr", "1", str(_MAX_ROWS),
        stat_code, cycle, s, e,
    ]
    for item_key in ("item_code1", "item_code2", "item_code3"):
        code = spec.get(item_key)
        if code:
            parts.append(str(code))
    url = "/".join(parts)

    try:
        data = _request(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ECOS 요청 실패(%s): %s", stat_code, exc)
        return None

    container = data.get("StatisticSearch")
    if not container or "row" not in container:
        # ECOS 는 에러도 200 으로 주고 RESULT 코드를 담는다.
        msg = data.get("RESULT", {}).get("MESSAGE") if isinstance(data, dict) else None
        logger.warning("ECOS 빈/에러 응답(%s): %s", stat_code, msg or data)
        return None

    rows = container["row"]
    idx, vals = [], []
    for r in rows:
        v = r.get("DATA_VALUE")
        if v in (None, "", "-"):
            continue
        try:
            value = float(v)
        except ValueError:
            continue
        idx.append(_parse_time(r["TIME"], cycle))
        vals.append(value)

    if not idx:
        return None

    series = pd.Series(vals, index=pd.DatetimeIndex(idx)).sort_index()
    series = series[~series.index.duplicated(keep="last")]
    return series
