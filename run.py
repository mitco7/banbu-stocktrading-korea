"""로컬 개발 서버 진입점.

    python run.py

uvicorn 으로 FastAPI 앱(api.main:app)을 reload 모드로 띄운다.
기본 바인딩은 로컬 전용(127.0.0.1)이며, 외부 인터페이스 노출이 필요할 때만
HOST=0.0.0.0 처럼 환경변수로 명시적으로 opt-in 한다(우발적 노출 방지).
운영 배포 시에는 reload 를 끄고 별도 프로세스 매니저(systemd 등)로 실행한다.
"""

import os

import uvicorn


def main() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    # RELOAD=false 로 끄면 운영 형태(단일 프로세스)로 실행. 운영 배포는 이 파일 대신
    # `uvicorn api.main:app --workers 1` + systemd 를 사용한다(워커 1 고정 — 스케줄러 중복 방지).
    reload = os.getenv("RELOAD", "true").strip().lower() in ("1", "true", "yes")
    uvicorn.run(
        "api.main:app",
        host=host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    main()
