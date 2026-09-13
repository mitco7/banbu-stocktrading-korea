-- predicted_stocks: ML 종목별 예측값 vs 실제값(날짜별).
-- predict.py 의 save_predictions_to_db() 가 delete 후 insert(매 실행 갱신).
-- 컬럼은 25종목(TARGET_COLUMNS) 기준 자동 생성. 유니버스 기준일: 2026-05-31
CREATE TABLE IF NOT EXISTS predicted_stocks (
    id BIGSERIAL PRIMARY KEY
    , "날짜" DATE NOT NULL
    , "삼성전자_Predicted" FLOAT8
    , "삼성전자_Actual" FLOAT8
    , "SK하이닉스_Predicted" FLOAT8
    , "SK하이닉스_Actual" FLOAT8
    , "LG에너지솔루션_Predicted" FLOAT8
    , "LG에너지솔루션_Actual" FLOAT8
    , "삼성바이오로직스_Predicted" FLOAT8
    , "삼성바이오로직스_Actual" FLOAT8
    , "현대차_Predicted" FLOAT8
    , "현대차_Actual" FLOAT8
    , "기아_Predicted" FLOAT8
    , "기아_Actual" FLOAT8
    , "현대모비스_Predicted" FLOAT8
    , "현대모비스_Actual" FLOAT8
    , "삼성물산_Predicted" FLOAT8
    , "삼성물산_Actual" FLOAT8
    , "NAVER_Predicted" FLOAT8
    , "NAVER_Actual" FLOAT8
    , "포스코홀딩스_Predicted" FLOAT8
    , "포스코홀딩스_Actual" FLOAT8
    , "LG화학_Predicted" FLOAT8
    , "LG화학_Actual" FLOAT8
    , "셀트리온_Predicted" FLOAT8
    , "셀트리온_Actual" FLOAT8
    , "한화에어로스페이스_Predicted" FLOAT8
    , "한화에어로스페이스_Actual" FLOAT8
    , "HD현대중공업_Predicted" FLOAT8
    , "HD현대중공업_Actual" FLOAT8
    , "두산에너빌리티_Predicted" FLOAT8
    , "두산에너빌리티_Actual" FLOAT8
    , "삼성전기_Predicted" FLOAT8
    , "삼성전기_Actual" FLOAT8
    , "카카오_Predicted" FLOAT8
    , "카카오_Actual" FLOAT8
    , "크래프톤_Predicted" FLOAT8
    , "크래프톤_Actual" FLOAT8
    , "하이브_Predicted" FLOAT8
    , "하이브_Actual" FLOAT8
    , "SK텔레콤_Predicted" FLOAT8
    , "SK텔레콤_Actual" FLOAT8
    , "에코프로머티_Predicted" FLOAT8
    , "에코프로머티_Actual" FLOAT8
    , "한국항공우주_Predicted" FLOAT8
    , "한국항공우주_Actual" FLOAT8
    , "LG전자_Predicted" FLOAT8
    , "LG전자_Actual" FLOAT8
    , "SK이노베이션_Predicted" FLOAT8
    , "SK이노베이션_Actual" FLOAT8
    , "HL만도_Predicted" FLOAT8
    , "HL만도_Actual" FLOAT8
);

CREATE INDEX IF NOT EXISTS idx_predicted_stocks_date ON predicted_stocks ("날짜");
ALTER TABLE predicted_stocks DISABLE ROW LEVEL SECURITY;
