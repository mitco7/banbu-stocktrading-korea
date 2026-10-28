"""LLM(Claude) 최종 거부권 검토 (Step 5).

역할: '한국 주식 트레이딩 20년 경력의 최종 의사결정자'. **거부권만** 행사 —
  BUY 후보를 HOLD 로 내릴 수는 있어도, 후보에 없던 새 종목 추가/탈락 종목 되살리기는 불가.
  (코드 레벨에서도 차단: scoring_service 가 LLM 이 명시적 BUY 한 '통과 종목'만 BUY 처리.)

견고성(CLAUDE.md §2.4 fail-close):
  - 모델 폴백: claude-opus-4-8 → claude-sonnet-4-6. 각 3회 재시도(지수 백오프), 429/5xx 처리.
  - opus-4-8 은 temperature 미전달(파라미터 제거됨), sonnet 은 temperature=0.
  - 프롬프트 캐싱: 시스템 프롬프트에 cache_control(ephemeral).
  - ANTHROPIC_API_KEY 없음/전 모델 실패/파싱 실패/타임아웃 — 어떤 경우든 (ok=False) 반환 → 매수 차단.
"""

from __future__ import annotations

import json
import time

from core.config import settings
from services import notification_service
from utils.logger import get_logger

logger = get_logger(__name__)

# 모델 폴백 순서(스펙). 모델 ID 는 시크릿 아님.
LLM_MODELS = ("claude-opus-4-8", "claude-sonnet-5")
# temperature 를 받는 모델만 명시(allow-list). opus-4-8/4.7·fable 계열은 sampling 파라미터 400.
# (음수조건 `!= opus-4-8` 은 새 모델 추가 시 잘못 temperature 를 보내 400 → 스킵될 위험.)
_TEMPERATURE_OK = set()
_MAX_RETRIES = 3
_BASE_DELAY = 2.0      # 지수 백오프 기준(초)
_MAX_TOKENS = 2000
_TIMEOUT = 60.0

SYSTEM_PROMPT = (
    "당신은 한국 주식(KOSPI/KOSDAQ) 트레이딩 20년 경력의 최종 의사결정자입니다. "
    "시스템이 데이터로 1차 선별·점수화한 '매수 후보'를 받아, 마지막으로 거부권을 행사합니다.\n\n"
    "■ 권한(엄격): 당신은 **거부권만** 가집니다. 후보의 BUY 를 HOLD 로 내릴 수는 있어도, "
    "목록에 없는 새 종목을 추가하거나 이미 탈락한 종목을 되살릴 수 없습니다. 받은 목록의 종목에 대해서만 판단하세요.\n\n"
    "■ 한국 시장 특화 검토 기준: 실적시즌·주요 공시 임박, 선물·옵션 만기일(쿼드러플 위칭), "
    "상·하한가(±30%) 근접, 공매도 이슈, 동일 섹터 과집중, 원/달러 급변, 글로벌 위험회피(VIX) 국면. "
    "현재 시장 변동성 국면(regime)과 직전 영업일 VIX 를 함께 고려하세요.\n\n"
    "■ 입력의 'ML 예측'은 14일 후 예측 '상승률(%)'이며 확률이 아닙니다. 가격은 전부 KRW.\n\n"
    "■ 출력: 오직 아래 JSON 만 출력하세요(설명·코드펜스 금지).\n"
    "{\n"
    '  "market_analysis": "오늘 시장 총평(한국어 2~4문장)",\n'
    '  "decisions": [ {"ticker": "종목코드", "decision": "BUY" 또는 "HOLD", "reason": "한국어 사유"} ]\n'
    "}\n"
    "받은 모든 종목에 대해 decisions 항목을 빠짐없이 포함하세요. 의심스러우면 HOLD(보수적)."
)


def _fmt(v: object, nd: int = 2) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _build_user_message(survivors: list[dict], vix_t1, regime: str, market_date: str) -> str:
    lines = [
        f"[시장 컨텍스트] 기준일={market_date} KST, 변동성 국면(regime)={regime}, "
        f"직전영업일 VIX[T-1]={_fmt(vix_t1)}.",
        f"[매수 후보 {len(survivors)}종목 — 종합점수 내림차순]",
    ]
    for s in survivors:
        di = f"+DI={_fmt(s.get('plus_di'))}/-DI={_fmt(s.get('minus_di'))}"
        flow = (
            f"flow_score={_fmt(s.get('flow_score'), 3)}, "
            f"외국인순매수={_fmt(s.get('foreign_net'), 0)}원, "
            f"기관순매수={_fmt(s.get('institution_net'), 0)}원, "
            f"쌍끌이(dual_buy)={'예' if s.get('dual_buy') else '아니오'}"
        )
        lines.append(
            f"\n#{s.get('rank')} {s.get('name')}({s.get('ticker')})\n"
            f"  - ML 예측: +{_fmt(s.get('predicted_rise_pct'))}% 상승률(확률 아님), "
            f"현재가 {_fmt(s.get('close_price'), 0)}원\n"
            f"  - 기술: 골든크로스={'예' if s.get('golden_cross') else '아니오'}, "
            f"RSI={_fmt(s.get('rsi'))}, MACD매수={'예' if s.get('macd_buy') else '아니오'}, "
            f"ATR%={_fmt(s.get('atr_pct'))}, ADX={_fmt(s.get('adx'))}({di}), "
            f"거래량비율={_fmt(s.get('volume_ratio'))}\n"
            f"  - 수급: {flow}\n"
            f"  - 종합점수={_fmt(s.get('total_score'), 4)} "
            f"(ml={_fmt(s.get('ml_sub'), 3)}, tech={_fmt(s.get('tech_sub'), 3)}, "
            f"flow={_fmt(s.get('flow_sub'), 3)}, vol={_fmt(s.get('vol_sub'), 3)})"
        )
    lines.append("\n위 후보에 대해 거부권을 행사해 BUY/HOLD 를 JSON 으로 결정하세요.")
    return "\n".join(lines)


def _parse_json(text: str) -> dict | None:
    """응답 텍스트에서 JSON 객체 추출(코드펜스/잡설 섞여도 첫 { ~ 마지막 } 시도)."""
    if not text:
        return None
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1 or j < i:
        return None
    try:
        return json.loads(text[i : j + 1])
    except json.JSONDecodeError:
        return None


def _call_model(client, anthropic_mod, model: str, user_msg: str):
    """단일 모델 호출 + 3회 재시도. 성공 시 (market_analysis, decisions) 반환, 실패 시 None."""
    kwargs = {
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_msg}],
    }
    if model in _TEMPERATURE_OK:      # sonnet 은 temperature=0, opus-4-8 은 미전달(파라미터 제거됨).
        kwargs["temperature"] = 0.0

    last_err: object = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = client.messages.create(**kwargs)
            text = "".join(b.text for b in resp.content if b.type == "text")
            parsed = _parse_json(text)
            if parsed is None:
                logger.warning("LLM(%s) JSON 파싱 실패(시도 %d/%d)", model, attempt + 1, _MAX_RETRIES)
            else:
                decisions: dict[str, dict] = {}
                for d in parsed.get("decisions", []):
                    t = d.get("ticker")
                    if not t:
                        continue
                    dec = d.get("decision") if d.get("decision") in ("BUY", "HOLD") else "HOLD"
                    decisions[str(t)] = {"decision": dec, "reason": d.get("reason")}
                logger.info("LLM 검토 성공: model=%s, 결정 %d건", model, len(decisions))
                return parsed.get("market_analysis"), decisions
            last_err = "parse_error"
        except anthropic_mod.RateLimitError as exc:
            last_err = exc
        except anthropic_mod.APIStatusError as exc:
            if exc.status_code >= 500:           # 500/503/529 등 → 재시도
                last_err = exc
            else:                                # 4xx(키/요청 오류) → 재시도 무의미, 다음 모델로
                logger.error("LLM(%s) 비재시도 오류 %s — 다음 모델", model, exc.status_code)
                return None
        except anthropic_mod.APIConnectionError as exc:
            last_err = exc
        except Exception as exc:  # noqa: BLE001 — 어떤 예외든 fail-close 경로 유지
            last_err = exc

        if attempt < _MAX_RETRIES - 1:
            delay = _BASE_DELAY * (2 ** attempt)
            logger.warning("LLM(%s) 재시도 %d/%d in %.1fs: %s", model, attempt + 1, _MAX_RETRIES, delay, last_err)
            time.sleep(delay)
    logger.warning("LLM(%s) 전 재시도 실패: %s", model, last_err)
    return None


def review(
    survivors: list[dict], vix_t1, regime: str, market_date: str
) -> tuple[bool, str | None, dict, str | None]:
    """LLM 거부권 검토. 반환: (ok, market_analysis, {ticker: {decision, reason}}, model).

    ok=False 면 fail-close — 호출자는 전 종목 매수 차단(HOLD)해야 한다.
    """
    if not survivors:
        return True, None, {}, None
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY 미설정 → LLM 검토 생략, fail-close(전 종목 HOLD).")
        return False, None, {}, None

    try:
        import anthropic
    except ImportError:
        logger.error("anthropic SDK 미설치 → fail-close.")
        return False, None, {}, None

    client = anthropic.Anthropic(
        api_key=settings.ANTHROPIC_API_KEY, timeout=_TIMEOUT, max_retries=0  # 재시도는 직접 제어
    )
    user_msg = _build_user_message(survivors, vix_t1, regime, market_date)

    for model in LLM_MODELS:
        result = _call_model(client, anthropic, model, user_msg)
        if result is not None:
            analysis, decisions = result
            return True, analysis, decisions, model

    logger.error("LLM 전 모델(%s) 실패 → fail-close(전 종목 매수 차단).", ", ".join(LLM_MODELS))
    notification_service.notify_error(
        "LLM 검토 실패 — 당일 매수 전면 차단(fail-close)",
        f"시도 모델: {', '.join(LLM_MODELS)}. 응답 실패/타임아웃/파싱 실패 — 전 종목 HOLD 처리.",
    )
    return False, None, {}, None
