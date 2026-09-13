-- stock_analysis_results: ML 종목별 예측 성능·추천 결과.
-- predict.py 의 save_analysis_to_db() 가 delete 후 insert(매 실행 갱신).
CREATE TABLE IF NOT EXISTS stock_analysis_results (
    id BIGSERIAL PRIMARY KEY,
    "Stock" TEXT,
    "MAE" FLOAT8,
    "MSE" FLOAT8,
    "RMSE" FLOAT8,
    "MAPE (%)" FLOAT8,
    "Accuracy (%)" FLOAT8,
    "Last Actual Price" FLOAT8,
    "Predicted Future Price" FLOAT8,
    "Predicted Rise" BOOLEAN,
    "Rise Probability (%)" FLOAT8,
    "Recommendation" TEXT,
    "Analysis" TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE stock_analysis_results DISABLE ROW LEVEL SECURITY;
