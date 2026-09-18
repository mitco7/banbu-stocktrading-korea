"""한국투자증권(KIS) OpenAPI 저수준 HTTP 클라이언트 (Step 6).

이 모듈만 KIS 와 직접 통신한다(레이어 분리). 상위 서비스는 도메인 함수로 호출한다.
- 읽기(시세/잔고)는 멱등 → @retry 적용. **주문(POST order)은 재시도 금지**(중복주문=최악, §2.7).
- 시크릿(appkey/appsecret/token)은 예외/로그에 마스킹(utils.crypto).
- 토큰은 auth_service 가 관리 — get/post 는 token 인자를 받거나 없으면 auth_service 에서 지연 조회.

⚠ TR_ID·엔드포인트는 prompt/10 의 작성시점 값. 운영 전 KIS 공식 문서로 재검증할 것.
"""

from __future__ import annotations

import threading
import time

import requests

from core.config import settings
from utils.crypto import mask_account, mask_secrets
from utils.logger import get_logger
from utils.retry import retry

logger = get_logger(__name__)

# HTTP 타임아웃 (연결, 읽기)초 — config 외부화. 모의서버(openapivts)가 장 후반(14:30~15:30)에
# 10초를 초과해 응답하는 패턴이 관측되어(2026-09 운영) 읽기는 15초. 연결은 정상 시 수 ms라 별도로 짧게.
_TIMEOUT = (float(settings.KIS_CONNECT_TIMEOUT_SEC), float(settings.KIS_TIMEOUT_SEC))

# ── 호출 페이싱(초당 건수 제한) ──────────────────────────────
# KIS 모의투자는 초당 2건 제한 — 초과 시 EGW00201 거절. 주문 POST 는 재시도 금지라
# 레이트리밋 거절 = 그 종목 매수/매도 실패로 직결되므로, 모든 KIS 호출을 여기서 직렬화한다.
_rate_lock = threading.Lock()
_last_call_at = 0.0


def _throttle() -> None:
    """직전 KIS 호출과의 최소 간격(1/KIS_RATE_LIMIT_PER_SEC 초)을 보장한다."""
    global _last_call_at
    min_interval = 1.0 / max(settings.KIS_RATE_LIMIT_PER_SEC, 0.1)
    with _rate_lock:
        wait = _last_call_at + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


class KisError(RuntimeError):
    """KIS API 오류(rt_cd != 0 또는 HTTP 오류)."""


class KisOrderUncertainError(KisError):
    """주문 POST 통신 예외 — 주문이 KIS 에 도달했는지 알 수 없는 상태.

    호출자는 이 예외를 '주문 실패'로 단정하고 재주문하면 안 된다(중복 주문 위험, §2.7).
    상태를 주문중으로 유지하고 reconcile 이 잔고로 실체를 확인하게 해야 한다.
    """


def _mask(text: str, token: str | None = None) -> str:
    """예외/로그 문자열의 시크릿·토큰·계좌번호 마스킹(§2.6). 잔고 조회 등은 계좌번호를 쿼리스트링에
    싣기 때문에 requests 예외 메시지(URL 포함)에 계좌번호가 그대로 들어간다. 토큰은 헤더 값 오류
    (InvalidHeader) 메시지에 'Bearer …' 로 통째로 실릴 수 있다."""
    text = mask_secrets(text, [settings.kis_appkey, settings.kis_appsecret, token])
    cano = settings.kis_cano
    return text.replace(cano, mask_account(cano)) if cano else text


def _common_headers(tr_id: str, token: str, hashkey: str | None = None) -> dict:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": settings.kis_appkey,
        "appsecret": settings.kis_appsecret,
        "tr_id": tr_id,
        "custtype": "P",  # 개인
    }
    if hashkey:
        headers["hashkey"] = hashkey
    return headers


def _token() -> str:
    """지연 조회 — 순환 import 방지(auth_service 가 본 모듈의 request_access_token 사용)."""
    from services import auth_service

    return auth_service.get_access_token()


def request_access_token() -> dict:
    """/oauth2/tokenP 로 접근토큰 신규 발급. auth_service 전용(토큰 불필요).

    Returns: {"access_token", "token_type", "expires_in", "access_token_token_expired"}
    """
    url = f"{settings.kis_active_url}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": settings.kis_appkey,
        "appsecret": settings.kis_appsecret,
    }
    try:
        resp = requests.post(url, json=body, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise KisError(f"토큰 발급 요청 실패: {_mask(str(exc))}") from None
    if resp.status_code != 200:
        raise KisError(f"토큰 발급 HTTP {resp.status_code}: {_mask(resp.text)[:200]}")
    data = resp.json()
    if "access_token" not in data:
        raise KisError(f"토큰 응답에 access_token 없음: {_mask(str(data))[:200]}")
    return data


def make_hashkey(body: dict) -> str | None:
    """주문 본문 무결성용 hashkey. 실패 시 None(best-effort — 모의는 미요구 경우 많음)."""
    url = f"{settings.kis_active_url}/uapi/hashkey"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "appkey": settings.kis_appkey,
        "appsecret": settings.kis_appsecret,
    }
    try:
        _throttle()
        resp = requests.post(url, json=body, headers=headers, timeout=_TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("HASH")
        logger.warning("hashkey 발급 HTTP %s — hashkey 없이 진행", resp.status_code)
    except requests.RequestException as exc:
        logger.warning("hashkey 발급 실패(무시): %s", _mask(str(exc)))
    return None


_GET_RETRYABLE = (KisError, requests.RequestException)


@retry(attempts=3, base_delay=1.0, backoff=2.0, exceptions=_GET_RETRYABLE)
def get(path: str, tr_id: str, params: dict, token: str | None = None) -> dict:
    """읽기 전용 GET(시세/잔고). 멱등 → 재시도 안전. rt_cd != 0 이면 KisError."""
    return _get_once(path, tr_id, params, token)


@retry(attempts=settings.SELL_QUOTE_ATTEMPTS, base_delay=1.0, backoff=2.0, exceptions=_GET_RETRYABLE)
def get_quick(path: str, tr_id: str, params: dict, token: str | None = None) -> dict:
    """재시도를 줄인 GET — 곧 다음 주기가 다시 확인하는 매도 감시(1분 주기)용."""
    return _get_once(path, tr_id, params, token)


def _get_once(path: str, tr_id: str, params: dict, token: str | None = None) -> dict:
    token = token or _token()
    url = f"{settings.kis_active_url}{path}"
    try:
        _throttle()
        resp = requests.get(url, headers=_common_headers(tr_id, token), params=params, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise KisError(f"GET {path} 실패: {_mask(str(exc), token)}") from None
    if resp.status_code != 200:
        raise KisError(f"GET {path} HTTP {resp.status_code}: {_mask(resp.text, token)[:200]}")
    data = resp.json()
    if str(data.get("rt_cd", "0")) != "0":
        raise KisError(f"GET {path} rt_cd={data.get('rt_cd')} {data.get('msg_cd')}: "
                       f"{_mask(str(data.get('msg1')), token)}")
    return data


def post_order(path: str, tr_id: str, body: dict, token: str | None = None) -> dict:
    """주문 POST. **재시도 절대 금지**(중복주문 방지 §2.7). 실패 시 KisError.

    호출자는 INSERT(buy_ordered) 등으로 의도를 먼저 기록한 뒤 이 함수를 1회만 호출해야 한다.
    """
    token = token or _token()
    url = f"{settings.kis_active_url}{path}"
    hashkey = make_hashkey(body)
    if hashkey is None and not settings.KIS_USE_MOCK:
        # 실전은 hashkey 필수 — 없으면 fail-close(본문 무결성 미보장 주문 송신 금지).
        raise KisError("실전 주문 hashkey 발급 실패 — 주문 차단(fail-close)")
    try:
        _throttle()
        resp = requests.post(
            url, headers=_common_headers(tr_id, token, hashkey), json=body, timeout=_TIMEOUT
        )
    except requests.ConnectTimeout as exc:
        # TCP 연결 자체가 안 열림 = 요청 바이트 미송신 → 주문 미도달 확정. 확정 실패로 올려야
        # 매도가 holding 으로 복귀해 다음 사이클이 재판정한다(불명 처리 시 당일 손절 재시도 불가).
        # ReadTimeout·SSLError 등은 본문 송신 뒤일 수 있어 아래 '불명'으로 둔다.
        raise KisError(f"주문 POST {path} 연결 실패(미전송 확정): {_mask(str(exc), token)}") from None
    except requests.RequestException as exc:
        # 네트워크 예외: 주문이 서버에 도달했는지 불확실 → 절대 재시도하지 말고 호출자가 reconcile 로 확인.
        raise KisOrderUncertainError(
            f"주문 POST {path} 통신 예외(체결여부 불명, 재시도 금지): {_mask(str(exc), token)}"
        ) from None
    if resp.status_code != 200:
        raise KisError(f"주문 POST {path} HTTP {resp.status_code}: {_mask(resp.text, token)[:200]}")
    data = resp.json()
    if str(data.get("rt_cd", "1")) != "0":
        raise KisError(f"주문 거부 {path} rt_cd={data.get('rt_cd')} {data.get('msg_cd')}: "
                       f"{_mask(str(data.get('msg1')), token)}")
    return data
