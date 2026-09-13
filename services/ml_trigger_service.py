"""Kaggle API로 ML 예측 노트북을 트리거하는 서비스.

핵심 기능:
  - check_auth():       Kaggle 인증 확인(kernels list 호출)
  - push_kernel():      노트북 push (= 새 버전 + 자동 실행 트리거)
  - get_status():       현재 실행 상태 조회 (queued/running/complete/error 등)
  - trigger_and_wait(): push 후 완료될 때까지 폴링 대기

인증:
  - .env 의 KAGGLE_API_TOKEN (또는 KAGGLE_USERNAME+KAGGLE_KEY) 를 subprocess 환경변수로 주입.

Secrets 주입:
  - Kaggle UserSecretsClient 는 API push 로 만든 kernel 버전에서 실패할 수 있어,
    push 직전에 predict.py 를 .ipynb 로 변환하며 .env 의 SUPABASE_URL_KOREA 와
    **SUPABASE_KAGGLE_KEY(anon)** 를 첫 셀 os.environ 으로 주입.
  - ⚠ 서버용 SUPABASE_KEY_KOREA(service_role)는 절대 주입하지 않는다 — RLS(sql/013) 도입 후
    전권 키라 Kaggle 평문 노출 시 DB 전체가 넘어간다. 키 미설정/동일 시 push 거부(fail-close).
  - 결과 predict.ipynb 는 secrets 가 들어가므로 .gitignore 처리(커밋 금지).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

# 폴링 간격 / 최대 대기 시간
POLL_INTERVAL_SEC = 10
MAX_WAIT_SEC = 900  # 15분

# 종료 상태로 간주하는 키워드들
TERMINAL_OK = {"complete"}
TERMINAL_ERR = {"error", "cancel_acknowledged", "cancel_requested"}


def _kernel_ref() -> str:
    """`username/slug` 형태의 kernel reference (실제 Kaggle username 기준)."""
    if not settings.KAGGLE_USERNAME:
        raise RuntimeError(
            "KAGGLE_USERNAME 이 .env 에 설정되지 않았습니다 "
            "(토큰 이름이 아닌 실제 Kaggle 계정 username 사용)"
        )
    return f"{settings.KAGGLE_USERNAME}/{settings.KAGGLE_KERNEL_SLUG}"


def _notebook_dir() -> Path:
    """kaggle_notebook 폴더 절대경로(프로젝트 루트 기준)."""
    p = Path(settings.KAGGLE_NOTEBOOK_DIR)
    if not p.is_absolute():
        # services/ml_trigger_service.py → parents[1] = 프로젝트 루트
        p = Path(__file__).resolve().parents[1] / p
    return p


def _kaggle_env() -> dict:
    """kaggle CLI 호출 시 주입할 환경변수(인증 + Windows UTF-8 강제).

    인증 우선순위:
      1. KAGGLE_API_TOKEN (신형 Access Token, "KGAT_" 접두사) — 단독 사용
      2. KAGGLE_USERNAME + KAGGLE_KEY (기존 32자리 hex) — 둘 다 필요
    """
    env = os.environ.copy()
    # Windows 콘솔 cp949 충돌 방지(predict.py 의 한글 변수명 등).
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    if settings.KAGGLE_API_TOKEN:
        env["KAGGLE_API_TOKEN"] = settings.KAGGLE_API_TOKEN
        if settings.KAGGLE_USERNAME:
            env["KAGGLE_USERNAME"] = settings.KAGGLE_USERNAME
        return env

    if settings.KAGGLE_USERNAME and settings.KAGGLE_KEY:
        env["KAGGLE_USERNAME"] = settings.KAGGLE_USERNAME
        env["KAGGLE_KEY"] = settings.KAGGLE_KEY
        return env

    raise RuntimeError(
        "Kaggle 인증 정보가 .env 에 없습니다. "
        "(KAGGLE_API_TOKEN 또는 KAGGLE_USERNAME+KAGGLE_KEY 둘 중 하나 설정 필요)"
    )


def _kaggle_bin() -> str:
    """kaggle CLI 실행 파일 절대경로 해석.

    systemd 등에서는 PATH 에 venv/bin 이 없어 'kaggle' 을 못 찾는다 →
    (1) PATH 의 which  (2) 현재 파이썬과 같은 폴더(venv/bin 또는 Scripts)
    (3) sys.prefix 기준 venv bin/Scripts 순으로 찾고, 없으면 'kaggle' 폴백.

    ⚠ sys.executable 에 resolve() 금지: 리눅스 venv 의 python3 은 시스템 인터프리터로의
      심링크라 resolve() 하면 /usr/bin 으로 탈출해 venv/bin 의 kaggle 을 놓친다
      (systemd 배포에서 실제 발생 — 2026-08-31 파이프라인 3단계 rc=127 사고).
    """
    found = shutil.which("kaggle")
    if found:
        return found
    search_dirs = [
        Path(sys.executable).parent,      # venv/bin (심링크 유지한 경로)
        Path(sys.prefix) / "bin",         # 리눅스/맥 venv
        Path(sys.prefix) / "Scripts",     # Windows venv
    ]
    for d in search_dirs:
        for name in ("kaggle", "kaggle.exe"):
            candidate = d / name
            if candidate.exists():
                return str(candidate)
    logger.warning("kaggle CLI 경로를 찾지 못함(PATH·%s) — 'kaggle' 이름으로 시도",
                   ", ".join(str(d) for d in search_dirs))
    return "kaggle"


def _run_kaggle_cmd(args: list, timeout: int = 60) -> tuple[int, str, str]:
    """kaggle CLI 실행. (returncode, stdout, stderr) 반환."""
    try:
        proc = subprocess.run(
            [_kaggle_bin()] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_kaggle_env(),
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", ("kaggle CLI 실행 파일을 찾지 못함 — venv 에 kaggle 이 설치돼 있어도 "
                         "실행 환경 PATH 에 venv/bin 이 없으면 발생. venv/bin/kaggle 존재 여부와 "
                         "_kaggle_bin() 탐색 로그를 확인할 것 (pip 미설치가 아닐 수 있음)")
    except subprocess.TimeoutExpired as e:
        return 124, "", f"kaggle 명령 타임아웃 ({timeout}초): {e}"


def check_auth() -> tuple[bool, str]:
    """Kaggle 인증이 정상 작동하는지 확인(kernels list 호출)."""
    try:
        rc, out, err = _run_kaggle_cmd(["kernels", "list", "-m", "--page-size", "1"])
    except RuntimeError as exc:  # 자격증명 미설정 등
        return False, str(exc)
    if rc == 0:
        return True, "Kaggle 인증 OK"
    return False, f"Kaggle 인증 실패 (rc={rc}): {err.strip() or out.strip()}"


def _build_ipynb_with_injected_secrets(py_path: Path, ipynb_path: Path) -> None:
    """predict.py → secrets 주입 셀을 prepend 한 .ipynb 로 변환.

    Kaggle UserSecretsClient 가 API push kernel 에서 실패하는 이슈 우회용.
    결과 ipynb 는 secrets 포함 → .gitignore 필수.
    """
    # 주입 키는 **반드시 SUPABASE_KAGGLE_KEY(anon)** — 서버용 SUPABASE_KEY_KOREA 는
    # service_role(전권, RLS 우회)이라 Kaggle 에 평문으로 올라가면 DB 전체가 노출된다.
    # 폴백 금지(fail-close §2.6): 키가 없으면 push 를 거부한다.
    kaggle_key = settings.SUPABASE_KAGGLE_KEY
    if not settings.SUPABASE_URL_KOREA or not kaggle_key:
        raise RuntimeError(
            "SUPABASE_URL_KOREA / SUPABASE_KAGGLE_KEY 가 .env 에 없습니다. "
            "Kaggle 주입용 anon 키가 없어 push 불가(서버용 service_role 키 주입은 금지)."
        )
    if kaggle_key == settings.SUPABASE_KEY_KOREA:
        raise RuntimeError(
            "SUPABASE_KAGGLE_KEY 가 서버용 SUPABASE_KEY_KOREA 와 동일합니다. "
            "service_role 키가 Kaggle 로 유출될 수 있어 push 중단 — anon 키로 분리하세요."
        )

    code = py_path.read_text(encoding="utf-8")

    secret_cell = {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "# AUTO-INJECTED by ml_trigger_service. Do NOT edit / do NOT commit.\n",
            "import os\n",
            f"os.environ['SUPABASE_URL_KOREA'] = {settings.SUPABASE_URL_KOREA!r}\n",
            f"os.environ['SUPABASE_KEY_KOREA'] = {kaggle_key!r}\n",
        ],
    }
    main_cell = {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": code.splitlines(keepends=True),
    }
    nb = {
        "cells": [secret_cell, main_cell],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    with open(ipynb_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)


def push_kernel() -> tuple[bool, str]:
    """노트북 push(= 새 버전 + 실행 트리거). push 직전 predict.py + .env secrets 로 노트북 재생성.

    생성 파일명은 kernel-metadata.json 의 `code_file` 을 따른다(단일 진실원).
    캐글이 기존 커널의 노트북을 슬러그 이름(<slug>.ipynb)으로 저장하므로, 기존 커널을
    업데이트하려면 code_file 이 그 이름과 일치해야 한다. 불일치 시 신형 CLI 가
    "Notebook not found" 로 실패한다.
    """
    nb_dir = _notebook_dir()
    if not nb_dir.exists():
        msg = f"노트북 폴더가 없음: {nb_dir} (kernel-metadata.json + predict.py 필요)"
        logger.error(msg)
        return False, msg
    meta_path = nb_dir / "kernel-metadata.json"
    if not meta_path.exists():
        msg = f"kernel-metadata.json 없음: {nb_dir}"
        logger.error(msg)
        return False, msg

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        msg = f"kernel-metadata.json 파싱 실패: {e}"
        logger.error(msg)
        return False, msg
    code_file = meta.get("code_file") or "predict.ipynb"
    if not code_file.endswith(".ipynb"):
        msg = f"code_file 이 .ipynb 가 아님: {code_file} (secrets 주입은 notebook 형식 필요)"
        logger.error(msg)
        return False, msg

    py_path = nb_dir / "predict.py"
    ipynb_path = nb_dir / code_file
    if not py_path.exists():
        msg = f"predict.py 없음: {py_path} (secrets 주입 위해 .py 원본 필요)"
        logger.error(msg)
        return False, msg

    try:
        _build_ipynb_with_injected_secrets(py_path, ipynb_path)
        logger.info("%s 재생성 완료 (secrets 주입됨)", code_file)
    except Exception as e:  # noqa: BLE001
        msg = f"ipynb 생성 실패: {e}"
        logger.error(msg)
        return False, msg

    rc, out, err = _run_kaggle_cmd(["kernels", "push", "-p", str(nb_dir)], timeout=120)
    out_msg = (out or "").strip()
    err_msg = (err or "").strip()
    if rc != 0:
        msg = f"Kaggle push 실패 (rc={rc}): {err_msg or out_msg}"
        logger.error(msg)
        return False, msg

    # 신형 Kaggle CLI(2.x)는 push 실패 시에도 rc=0 을 반환하고 stdout 에 에러를 찍는다
    # (예: "Kernel push error: Notebook not found"). 성공 신호가 없으면 실패로 간주한다.
    if "successfully pushed" not in out_msg.lower():
        msg = f"Kaggle push 실패: {out_msg or err_msg or '알 수 없는 오류'}"
        logger.error(msg)
        return False, msg

    logger.info("Kaggle 노트북 push 성공: %s", out_msg)
    return True, out_msg


def get_status() -> str:
    """현재 실행 상태 조회. complete/running/queued/error/cancel_*/unavailable/unknown.

    주의: 신형 Kaggle CLI(2.x)의 status 엔드포인트(GetKernelSessionStatus)가
    서버측 500 을 반환하는 경우가 있다 → 이때는 "unavailable"(엔드포인트 장애)로 구분해
    반환한다. 진행 상황은 Kaggle 웹 UI 또는 결과 테이블(stock_analysis_results)로 확인.
    """
    rc, out, err = _run_kaggle_cmd(["kernels", "status", _kernel_ref()])
    text = (out + " " + err).lower()
    if rc != 0:
        logger.warning("status 조회 실패 (rc=%d): %s", rc, err.strip() or out.strip())
        # 5xx = Kaggle 서버측 엔드포인트 장애(권한/존재 문제 아님)
        if any(code in text for code in ("500 server error", "502 ", "503 ", "internal server error")):
            return "unavailable"
        return "unknown"
    for state in ("complete", "error", "cancel_acknowledged", "cancel_requested",
                  "running", "queued"):
        if state in text:
            return state
    return "unknown"


def trigger_and_wait(
    poll_interval: int = POLL_INTERVAL_SEC, max_wait: int = MAX_WAIT_SEC
) -> tuple[bool, str, dict]:
    """push 로 트리거 → 완료될 때까지 폴링. (success, message, meta) 반환."""
    start = time.time()

    pushed, push_msg = push_kernel()
    if not pushed:
        return False, push_msg, {"elapsed_sec": 0, "final_status": "push_failed", "push_output": push_msg}

    logger.info("Kaggle 실행 시작 — 완료 대기 중 (최대 %d초)", max_wait)
    last_status: str | None = None

    while True:
        elapsed = int(time.time() - start)
        if elapsed > max_wait:
            msg = f"Kaggle 실행 타임아웃 ({max_wait}초)"
            logger.error(msg)
            return False, msg, {"elapsed_sec": elapsed, "final_status": "timeout", "push_output": push_msg}

        time.sleep(poll_interval)
        status = get_status()
        if status != last_status:
            logger.info("  [%ds] 상태: %s", elapsed, status)
            last_status = status

        if status in TERMINAL_OK:
            msg = f"Kaggle 실행 완료 ({elapsed}초)"
            logger.info(msg)
            return True, msg, {"elapsed_sec": elapsed, "final_status": status, "push_output": push_msg}
        if status in TERMINAL_ERR:
            msg = f"Kaggle 실행 실패: {status}"
            logger.error(msg)
            return False, msg, {"elapsed_sec": elapsed, "final_status": status, "push_output": push_msg}
        # running / queued / unknown → 계속 대기
