"""한국 주식 매매 유니버스 (고정 — 자동 생성 파일).

이 파일은 scripts/select_universe.py 가 생성/덮어쓴다. 직접 편집하지 말 것.
종목 교체(수동 리밸런싱)는 select_universe.py 의 FIXED_UNIVERSE 를 수정한 뒤
`python scripts/select_universe.py --write` 로 재생성한다(CLAUDE.md §2.3 수동 거버넌스).

선정 기준일(SELECTED_AT): 2026-05-31
생성 시각              : 2026-05-31 10:44:06 KST
데이터 소스            : FDR 스냅샷(기준일 1일치 거래대금 proxy; N=20일 평균은 pykrx 사용 시 적용)
선정 방식              : 확정 유니버스는 사람이 정의(권위). 시총+거래대금 자동 상위25는
                         참고용으로만 산출(rank_method=rank-sum, N=20거래일). 자동 교체 없음.

종목별 시총·거래대금 메모(선정 기준일 기준, 참고용):
#    1. 005930 삼성전자           KOSPI  시총=18,532,703억  거래대금=116,996억
#    2. 000660 SK하이닉스         KOSPI  시총=16,627,346억  거래대금=198,649억
#    3. 373220 LG에너지솔루션       KOSPI  시총=1,071,720억  거래대금=5,841억
#    4. 207940 삼성바이오로직스       KOSPI  시총=630,946억  거래대금=1,742억
#    5. 005380 현대차            KOSPI  시총=1,480,399억  거래대금=22,300억
#    6. 000270 기아             KOSPI  시총=660,579억  거래대금=4,305억
#    7. 012330 현대모비스          KOSPI  시총=696,826억  거래대금=14,823억
#    8. 028260 삼성물산           KOSPI  시총=701,375억  거래대금=4,501억
#    9. 035420 NAVER          KOSPI  시총=367,085억  거래대금=17,681억
#   10. 005490 포스코홀딩스         KOSPI  시총=335,588억  거래대금=3,244억
#   11. 051910 LG화학           KOSPI  시총=259,074억  거래대금=3,360억
#   12. 068270 셀트리온           KOSPI  시총=428,474억  거래대금=2,182억
#   13. 012450 한화에어로스페이스      KOSPI  시총=604,839억  거래대금=4,107억
#   14. 329180 HD현대중공업        KOSPI  시총=730,530억  거래대금=5,714억
#   15. 034020 두산에너빌리티        KOSPI  시총=676,433억  거래대금=4,852억
#   16. 009150 삼성전기           KOSPI  시총=1,588,735억  거래대금=61,839억
#   17. 035720 카카오            KOSPI  시총=185,289억  거래대금=3,144억
#   18. 259960 크래프톤           KOSPI  시총=118,803억  거래대금=1,010억
#   19. 352820 하이브            KOSPI  시총=94,825억  거래대금=1,782억
#   20. 017670 SK텔레콤          KOSPI  시총=216,079억  거래대금=1,963억
#   21. 450080 에코프로머티         KOSPI  시총=43,979억  거래대금=317억
#   22. 047810 한국항공우주         KOSPI  시총=164,051억  거래대금=1,768억
#   23. 066570 LG전자           KOSPI  시총=477,252억  거래대금=25,482억
#   24. 096770 SK이노베이션        KOSPI  시총=201,511억  거래대금=1,131억
#   25. 204320 HL만도           KOSPI  시총=29,160억  거래대금=554억
"""

from __future__ import annotations

# (종목코드, 종목명, 시장) — 코드는 6자리 문자열(앞자리 0 보존).
KR_UNIVERSE: list[tuple[str, str, str]] = [
    ("005930", "삼성전자", "KOSPI"),
    ("000660", "SK하이닉스", "KOSPI"),
    ("373220", "LG에너지솔루션", "KOSPI"),
    ("207940", "삼성바이오로직스", "KOSPI"),
    ("005380", "현대차", "KOSPI"),
    ("000270", "기아", "KOSPI"),
    ("012330", "현대모비스", "KOSPI"),
    ("028260", "삼성물산", "KOSPI"),
    ("035420", "NAVER", "KOSPI"),
    ("005490", "포스코홀딩스", "KOSPI"),
    ("051910", "LG화학", "KOSPI"),
    ("068270", "셀트리온", "KOSPI"),
    ("012450", "한화에어로스페이스", "KOSPI"),
    ("329180", "HD현대중공업", "KOSPI"),
    ("034020", "두산에너빌리티", "KOSPI"),
    ("009150", "삼성전기", "KOSPI"),
    ("035720", "카카오", "KOSPI"),
    ("259960", "크래프톤", "KOSPI"),
    ("352820", "하이브", "KOSPI"),
    ("017670", "SK텔레콤", "KOSPI"),
    ("450080", "에코프로머티", "KOSPI"),
    ("047810", "한국항공우주", "KOSPI"),
    ("066570", "LG전자", "KOSPI"),
    ("096770", "SK이노베이션", "KOSPI"),
    ("204320", "HL만도", "KOSPI"),
]

# 선정 기준일(수동 리밸런싱 이력 추적용).
SELECTED_AT: str = "2026-05-31"

# 조회용 매핑.
CODE_TO_NAME: dict[str, str] = {code: name for code, name, _ in KR_UNIVERSE}
NAME_TO_CODE: dict[str, str] = {name: code for code, name, _ in KR_UNIVERSE}
CODE_TO_MARKET: dict[str, str] = {code: market for code, _, market in KR_UNIVERSE}

# ML/분석에서 사용할 종목 컬럼명(종목명 기준 통일). 데이터 조인은 코드 기준 권장.
TARGET_COLUMNS: list[str] = [name for _, name, _ in KR_UNIVERSE]

_VALID_MARKETS = {"KOSPI", "KOSDAQ"}


def validate_universe() -> None:
    """유니버스 무결성 검사. 실패 시 ValueError 발생.

    - 종목코드가 6자리 숫자 문자열인지
    - 코드 중복이 없는지
    - 시장 값이 KOSPI/KOSDAQ 중 하나인지
    - 파생 매핑/컬럼이 KR_UNIVERSE 와 일관적인지
    """
    seen: set[str] = set()
    for code, name, market in KR_UNIVERSE:
        if not (isinstance(code, str) and len(code) == 6 and code.isdigit()):
            raise ValueError(f"종목코드는 6자리 숫자 문자열이어야 합니다: {code!r} ({name})")
        if market not in _VALID_MARKETS:
            raise ValueError(f"시장 값이 올바르지 않습니다: {market!r} ({code} {name})")
        if code in seen:
            raise ValueError(f"중복 종목코드: {code} ({name})")
        seen.add(code)

    if len(KR_UNIVERSE) != len(TARGET_COLUMNS):
        raise ValueError("KR_UNIVERSE 와 TARGET_COLUMNS 길이가 다릅니다.")
    if set(CODE_TO_NAME) != seen or set(CODE_TO_MARKET) != seen:
        raise ValueError("CODE_TO_NAME / CODE_TO_MARKET 가 KR_UNIVERSE 와 불일치합니다.")
    if len(NAME_TO_CODE) != len(KR_UNIVERSE):
        raise ValueError("종목명 중복으로 NAME_TO_CODE 가 손실되었습니다.")


if __name__ == "__main__":
    validate_universe()
    print(f"OK — {len(KR_UNIVERSE)}종목, SELECTED_AT={SELECTED_AT}")
