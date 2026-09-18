"""KIS 클라이언트 검증 — 로그 마스킹(계좌번호·토큰), 연결/읽기 타임아웃 분리, 재시도 횟수,
주문 통신 예외 분류(미전송 확정 vs 체결여부 불명)."""

from __future__ import annotations

import logging

import pytest
import requests

from core.config import settings
from services import balance_service, kis_client
from utils import retry as retry_mod


@pytest.fixture
def offline_kis(monkeypatch):
    """네트워크 없이 kis_client 요청 경로를 태우는 공통 설정(모의 계좌·고정 토큰·페이싱/대기 생략)."""
    monkeypatch.setattr(settings, "KIS_USE_MOCK", True)
    monkeypatch.setattr(settings, "KIS_MOCK_CANO", "12345678")
    monkeypatch.setattr(kis_client, "_token", lambda: "tok-secret-value")
    monkeypatch.setattr(kis_client, "_throttle", lambda: None)
    monkeypatch.setattr(kis_client, "make_hashkey", lambda body: "hash")
    monkeypatch.setattr(retry_mod.time, "sleep", lambda s: None)


def test_retry_log_line_masks_account_and_token(offline_kis, monkeypatch, caplog):
    """9/18 사고 재현 — 재시도 경고 로그에 계좌번호·토큰이 평문으로 남으면 안 된다(§2.6)."""
    def fail(*a, **k):
        raise requests.ConnectionError(
            "Max retries exceeded with url: /uapi/domestic-stock/v1/trading/inquire-balance"
            "?CANO=12345678&ACNT_PRDT_CD=01 (header 'Bearer tok-secret-value')")
    monkeypatch.setattr(kis_client.requests, "get", fail)
    with caplog.at_level(logging.WARNING), pytest.raises(kis_client.KisError):
        kis_client.get_quick("/p", "TR", {})
    assert "실패(1/" in caplog.text                      # 재시도 경고 줄이 실제로 찍혔고
    assert "12345678" not in caplog.text and "tok-secret-value" not in caplog.text
    assert "******78" in caplog.text


def test_order_connect_timeout_is_definite_failure(offline_kis, monkeypatch):
    """TCP 연결조차 안 열렸으면 주문 미전송 확정 — KisError(불명 아님)여야 매도가 holding 으로
    복귀해 다음 사이클이 손절을 재시도한다."""
    def connect_timeout(*a, **k):
        raise requests.ConnectTimeout("connect timeout=5.0")
    monkeypatch.setattr(kis_client.requests, "post", connect_timeout)
    with pytest.raises(kis_client.KisError) as ei:
        kis_client.post_order("/order", "TR", {"PDNO": "005930"})
    assert not isinstance(ei.value, kis_client.KisOrderUncertainError)


def test_order_read_timeout_stays_uncertain(offline_kis, monkeypatch):
    """본문 송신 뒤일 수 있는 예외(읽기 타임아웃)는 체결여부 불명 — 재전송 금지(§2.7)."""
    def read_timeout(*a, **k):
        raise requests.ReadTimeout("read timeout=15.0")
    monkeypatch.setattr(kis_client.requests, "post", read_timeout)
    with pytest.raises(kis_client.KisOrderUncertainError):
        kis_client.post_order("/order", "TR", {"PDNO": "005930"})


def test_mask_hides_account_number_in_request_urls(monkeypatch):
    """requests 예외 메시지엔 쿼리스트링(CANO 포함) URL 이 통째로 들어간다 — 계좌번호 평문 금지(§2.6)."""
    monkeypatch.setattr(settings, "KIS_USE_MOCK", True)
    monkeypatch.setattr(settings, "KIS_MOCK_CANO", "12345678")
    msg = ("Max retries exceeded with url: /uapi/domestic-stock/v1/trading/inquire-balance"
           "?CANO=12345678&ACNT_PRDT_CD=01")
    masked = kis_client._mask(msg)
    assert "12345678" not in masked
    assert "CANO=******78" in masked


def test_mask_without_account_number_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "KIS_USE_MOCK", True)
    monkeypatch.setattr(settings, "KIS_MOCK_CANO", "")
    assert kis_client._mask("GET /x 실패") == "GET /x 실패"


def test_timeout_splits_connect_and_read():
    """연결은 짧게(정상 시 수 ms), 읽기는 모의서버 장후반 지연을 견디게 — requests (connect, read) 튜플."""
    assert kis_client._TIMEOUT == (settings.KIS_CONNECT_TIMEOUT_SEC, settings.KIS_TIMEOUT_SEC)
    assert settings.KIS_CONNECT_TIMEOUT_SEC < settings.KIS_TIMEOUT_SEC


@pytest.fixture
def failing_get(monkeypatch):
    """_get_once 가 매번 실패하도록 하고 호출 횟수를 센다(재시도 대기는 생략)."""
    calls = {"n": 0}

    def boom(path, tr_id, params, token=None):
        calls["n"] += 1
        raise kis_client.KisError("timeout")
    monkeypatch.setattr(kis_client, "_get_once", boom)
    monkeypatch.setattr(retry_mod.time, "sleep", lambda s: None)
    return calls


def test_get_retries_three_times(failing_get):
    with pytest.raises(kis_client.KisError):
        kis_client.get("/p", "TR", {})
    assert failing_get["n"] == 3


def test_get_quick_uses_reduced_attempts(failing_get):
    """매도 감시용 — 다음 사이클이 곧 재확인하므로 재시도를 줄여 장애일 사이클 지연을 막는다."""
    with pytest.raises(kis_client.KisError):
        kis_client.get_quick("/p", "TR", {})
    assert failing_get["n"] == settings.SELL_QUOTE_ATTEMPTS


def test_fetch_price_quick_routes_to_get_quick(monkeypatch):
    used: list[str] = []
    out = {"output": {"stck_prpr": "1000", "stck_sdpr": "990", "prdy_ctrt": "1.0", "acml_vol": "5"}}
    monkeypatch.setattr(kis_client, "get", lambda *a, **k: used.append("get") or out)
    monkeypatch.setattr(kis_client, "get_quick", lambda *a, **k: used.append("quick") or out)
    assert balance_service.fetch_price("005930")["price"] == 1000.0
    assert balance_service.fetch_price("005930", quick=True)["price"] == 1000.0
    assert used == ["get", "quick"]
