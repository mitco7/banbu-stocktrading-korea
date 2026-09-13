"""KIS 계좌 조회 — 잔고/매수가능/현재가 (Step 6, 읽기 전용).

모두 멱등 읽기라 kis_client.get(@retry) 사용. TR_ID 는 모의/실전 분기.
⚠ 엔드포인트·TR_ID·응답 필드명은 prompt/10 작성시점 값 — 운영 전 KIS 공식 문서로 재검증.
"""

from __future__ import annotations

from core.config import settings
from services import kis_client
from utils.logger import get_logger

logger = get_logger(__name__)

_BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
_PSBL_PATH = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"
_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"


def _tr(real_id: str, mock_id: str) -> str:
    return mock_id if settings.KIS_USE_MOCK else real_id


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_balance() -> dict:
    """잔고조회 → {holdings: {ticker: qty}, cash: 예수금, raw_holdings: [output1...]}.

    중복매수 방지용 보유 집합과 매수 배분 기준 현금을 함께 반환.
    """
    params = {
        "CANO": settings.kis_cano,
        "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
        "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
    }
    data = kis_client.get(_BALANCE_PATH, _tr("TTTC8434R", "VTTC8434R"), params)
    holdings: dict[str, int] = {}
    raw = data.get("output1") or []
    for row in raw:
        code = row.get("pdno")
        qty = int(_f(row.get("hldg_qty")))
        if code and qty > 0:
            holdings[code] = qty
    out2 = data.get("output2") or [{}]
    cash = _f(out2[0].get("dnca_tot_amt"))  # 예수금총금액
    return {"holdings": holdings, "cash": cash, "raw_holdings": raw}


def fetch_orderable(ticker: str, price: int) -> dict:
    """매수가능조회 → {cash: 주문가능현금, max_qty: 최대매수수량}."""
    params = {
        "CANO": settings.kis_cano,
        "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
        "PDNO": ticker,
        "ORD_UNPR": str(int(price)),
        "ORD_DVSN": "00",  # 지정가
        "CMA_EVLU_AMT_ICLD_YN": "N",
        "OVRS_ICLD_YN": "N",
    }
    data = kis_client.get(_PSBL_PATH, _tr("TTTC8908R", "VTTC8908R"), params)
    out = data.get("output") or {}
    return {"cash": _f(out.get("ord_psbl_cash")), "max_qty": int(_f(out.get("max_buy_qty")))}


def fetch_price(ticker: str) -> dict:
    """현재가 조회 → {price: 현재가, prev_close: 기준가(전일종가), change_pct: 전일대비율, volume: 누적거래량}."""
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
    data = kis_client.get(_PRICE_PATH, "FHKST01010100", params)
    out = data.get("output") or {}
    return {
        "price": _f(out.get("stck_prpr")),       # 현재가
        "prev_close": _f(out.get("stck_sdpr")),  # 기준가(전일종가)
        "change_pct": _f(out.get("prdy_ctrt")),  # 전일대비율(%)
        "volume": _f(out.get("acml_vol")),       # 누적거래량
    }
