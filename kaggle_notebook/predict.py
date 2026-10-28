"""
주가 예측 모델 (Transformer) - Kaggle 실행용

predict_colab.py 를 Kaggle 환경에 맞춰 정리한 버전:
  - !pip install ... 라인 제거 (subprocess 로 명시 설치)
  - SUPABASE_URL_KOREA / SUPABASE_KEY_KOREA 를 Kaggle Secrets 환경변수에서 로드
"""
import os
import sys
import time
import subprocess

# Kaggle 이미지에 supabase 가 없을 수 있어 명시 설치
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "supabase"])

import pandas as pd
import numpy as np
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import mean_absolute_error, mean_squared_error
from supabase import create_client, Client

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

import tensorflow as tf
from tensorflow.keras.models import Model
from tensorflow.keras.layers import (
    Input, Dense, Dropout, LayerNormalization, MultiHeadAttention, Add, GlobalAveragePooling1D
)
from tensorflow.keras.optimizers import Adam
import matplotlib.pyplot as plt

# ============================================================
# 환경 확인
# ============================================================
def check_environment():
    print("=" * 60)
    print("환경 확인")
    print("=" * 60)
    print(f"  Python: {os.sys.version.split()[0]}")
    print(f"  TensorFlow: {tf.__version__}")
    gpus = tf.config.list_physical_devices('GPU')
    if gpus:
        print(f"  GPU: {len(gpus)}개 감지")
        for gpu in gpus:
            print(f"    - {gpu.name}")
    else:
        print("  GPU: 없음 (CPU로 실행)")
    print()

# ============================================================
# Supabase 연결 (Kaggle Secrets 에서 로드)
# ============================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL_KOREA")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY_KOREA")

# Kaggle Secrets API 폴백 (UserSecretsClient)
if not SUPABASE_URL or not SUPABASE_KEY:
    try:
        from kaggle_secrets import UserSecretsClient
        user_secrets = UserSecretsClient()
        SUPABASE_URL = SUPABASE_URL or user_secrets.get_secret("SUPABASE_URL_KOREA")
        SUPABASE_KEY = SUPABASE_KEY or user_secrets.get_secret("SUPABASE_KEY_KOREA")
    except Exception as e:
        print(f"  UserSecretsClient 로드 실패: {e}")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "SUPABASE_URL_KOREA / SUPABASE_KEY_KOREA 환경변수가 설정되지 않았습니다. "
        "Kaggle 노트북의 Add-ons → Secrets 에 등록하세요."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# 설정값
# ============================================================
LOOKBACK = 90
FORECAST_HORIZON = 14
EPOCHS = 50
BATCH_SIZE = 32
LEARNING_RATE = 0.0001


TARGET_COLUMNS = [
    '삼성전자', 'SK하이닉스', 'LG에너지솔루션', '삼성바이오로직스', '현대차',
    '기아', '현대모비스', '삼성물산', 'NAVER', '포스코홀딩스',
    'LG화학', '셀트리온', '한화에어로스페이스', 'HD현대중공업', '두산에너빌리티',
    '삼성전기', '카카오', '크래프톤', '하이브', 'SK텔레콤',
    '에코프로머티', '한국항공우주', 'LG전자', 'SK이노베이션', 'HL만도',
]

ECONOMIC_FEATURES = [
    'KOSPI', 'KOSDAQ', '원/달러 환율', '달러 인덱스', '미국 달러지수(무역가중)',
    '엔/원 환율', '달러/엔', '달러/위안', 'WTI 유가', '금 가격',
    '필라델피아 반도체지수', '나스닥 종합지수', '나스닥 100', 'S&P 500 지수', '다우존스 지수',
    'VIX 지수', '닛케이 225', '상해종합', '항셍', '영국 FTSE',
    '독일 DAX', '프랑스 CAC 40', '미국 채권 ETF', '물가연동채 ETF', '투자등급 회사채 ETF',
    '미국 리츠 ETF', '러셀 2000 ETF', '미국 기준금리', '미국 10년물 국채금리', '미국 2년물 국채금리',
    '장단기 금리차', '기대 인플레이션', '미국 소비자물가지수', '미국 실업률', '미시간 소비자심리지수',
    '미국 M2 통화량', '미국 개인소비지출', '미국 금융스트레스지수', '미국 모기지금리', '미국 GDP',
    '미국 가계부채비율', '한국 기준금리', '한국 소비자물가지수', '수출금액지수', '경기선행지수',
    '산업생산지수',
]

# ============================================================
# 데이터 로드
# ============================================================
def get_all_data(table_name):
    all_data = []
    offset = 0
    limit = 1000
    while True:
        response = supabase.table(table_name).select("*").order("날짜", desc=False).limit(limit).offset(offset).execute()
        data = response.data
        if not data:
            break
        all_data.extend(data)
        offset += limit
    return all_data


def get_stock_data_from_db():
    try:
        all_data = get_all_data("market_data")
        print(f"  market_data: {len(all_data)}개 레코드")
        df = pd.DataFrame(all_data)

        df['날짜'] = pd.to_datetime(df['날짜'])
        df.sort_values(by='날짜', inplace=True)
        df.reset_index(drop=True, inplace=True)

        # 레거시 달력일(주말) 행 제거. LOOKBACK/14는 영업일 행 기준(CLAUDE.md §2.1).
        # 한국 휴장 평일은 수집기(market_calendar)에서 제외 — 잔존 시 소수.
        before = len(df)
        df = df[df['날짜'].dt.weekday < 5].copy()
        df.reset_index(drop=True, inplace=True)
        dropped_we = before - len(df)
        if dropped_we:
            print(f"  주말 행 제거: {dropped_we}개 (영업일 축)")

        # ffill만 허용. bfill 금지(신상장 종목 상장 전 구간 look-ahead).
        df = df.ffill()

        exclude_columns = ['날짜', 'id']
        numeric_columns = [col for col in df.columns if col not in exclude_columns]
        df[numeric_columns] = df[numeric_columns].apply(pd.to_numeric, errors='coerce')

        nan_ratios = df[numeric_columns].isna().mean()
        valid_columns = [col for col in numeric_columns if nan_ratios[col] < 1.0]
        df.dropna(subset=valid_columns, inplace=True)
        df.reset_index(drop=True, inplace=True)

        print(f"  전처리 후: {df.shape[0]}일 x {df.shape[1]}컬럼")
        return df
    except Exception as e:
        print(f"  데이터 로드 오류: {e}")
        return None

# ============================================================
# Transformer 모델
# ============================================================
def transformer_encoder(inputs, num_heads, ff_dim, dropout=0.1):
    attention_output = MultiHeadAttention(num_heads=num_heads, key_dim=inputs.shape[-1])(inputs, inputs)
    attention_output = Dropout(dropout)(attention_output)
    attention_output = Add()([inputs, attention_output])
    attention_output = LayerNormalization(epsilon=1e-6)(attention_output)

    ffn = Dense(ff_dim, activation="relu")(attention_output)
    ffn = Dense(inputs.shape[-1])(ffn)
    ffn_output = Dropout(dropout)(ffn)
    ffn_output = Add()([attention_output, ffn_output])
    ffn_output = LayerNormalization(epsilon=1e-6)(ffn_output)
    return ffn_output


def build_transformer_with_two_inputs(stock_shape, econ_shape, num_heads, ff_dim, target_size):
    stock_inputs = Input(shape=stock_shape)
    stock_encoded = stock_inputs
    for _ in range(4):
        stock_encoded = transformer_encoder(stock_encoded, num_heads=num_heads, ff_dim=ff_dim)
    stock_encoded = Dense(64, activation="relu")(stock_encoded)

    econ_inputs = Input(shape=econ_shape)
    econ_encoded = econ_inputs
    for _ in range(4):
        econ_encoded = transformer_encoder(econ_encoded, num_heads=num_heads, ff_dim=ff_dim)
    econ_encoded = Dense(64, activation="relu")(econ_encoded)

    merged = Add()([stock_encoded, econ_encoded])
    merged = Dense(128, activation="relu")(merged)
    merged = Dropout(0.2)(merged)
    merged = GlobalAveragePooling1D()(merged)
    outputs = Dense(target_size)(merged)

    return Model(inputs=[stock_inputs, econ_inputs], outputs=outputs)

# ============================================================
# 평가 함수
# ============================================================
def evaluate_predictions(data, target_columns, forecast_horizon):
    metrics = []
    for col in target_columns:
        predicted_col = f'{col}_Predicted'
        actual_col = f'{col}_Actual'

        if predicted_col not in data.columns or actual_col not in data.columns:
            continue

        predicted = data[predicted_col]
        actual = data[actual_col].shift(-forecast_horizon)

        valid_idx = ~predicted.isna() & ~actual.isna()
        predicted = predicted[valid_idx]
        actual = actual[valid_idx]

        if len(predicted) == 0:
            continue

        mae = mean_absolute_error(actual, predicted)
        mse = mean_squared_error(actual, predicted)
        rmse = mse ** 0.5
        mape = (abs((actual - predicted) / actual).mean()) * 100
        accuracy = 100 - mape

        metrics.append({
            'Stock': col,
            'MAE': round(mae, 4),
            'MSE': round(mse, 4),
            'RMSE': round(rmse, 4),
            'MAPE (%)': round(mape, 4),
            'Accuracy (%)': round(accuracy, 4)
        })

    return pd.DataFrame(metrics)


def analyze_rise_predictions(data, target_columns):
    last_row = data.iloc[-1]
    results = []

    for col in target_columns:
        actual_col = f'{col}_Actual'
        predicted_col = f'{col}_Predicted'

        last_actual_price = last_row.get(actual_col, np.nan)
        predicted_future_price = last_row.get(predicted_col, np.nan)

        if pd.notna(last_actual_price) and pd.notna(predicted_future_price):
            predicted_rise = predicted_future_price > last_actual_price
            rise_probability = ((predicted_future_price - last_actual_price) / last_actual_price) * 100
        else:
            predicted_rise = np.nan
            rise_probability = np.nan

        results.append({
            'Stock': col,
            'Last Actual Price': last_actual_price,
            'Predicted Future Price': predicted_future_price,
            'Predicted Rise': predicted_rise,
            'Rise Probability (%)': rise_probability
        })

    return pd.DataFrame(results)


def generate_recommendation(row):
    rise_prob = row.get('Rise Probability (%)', 0)
    predicted_rise = row.get('Predicted Rise', False)
    if pd.isna(rise_prob) or pd.isna(predicted_rise):
        return "No Data"
    if predicted_rise and rise_prob > 0:
        return "STRONG BUY" if rise_prob > 2 else "BUY"
    return "SELL"


def generate_analysis(row):
    stock_name = row['Stock']
    rise_prob = row.get('Rise Probability (%)', 0)
    predicted_rise = row.get('Predicted Rise', False)
    if pd.isna(rise_prob) or pd.isna(predicted_rise):
        return f"{stock_name}: Not enough data"
    if predicted_rise:
        return f"{stock_name} is expected to rise by about {rise_prob:.2f}%. Consider buying or holding."
    return f"{stock_name} is expected to fall by about {-rise_prob:.2f}%. A cautious approach is recommended."

# ============================================================
# Supabase 저장
# ============================================================
def save_predictions_to_db(result_df):
    try:
        records = result_df.to_dict('records')
        supabase.table("predicted_stocks").delete().neq("id", 0).execute()
        for i in range(0, len(records), 100):
            supabase.table("predicted_stocks").insert(records[i:i+100]).execute()
        print(f"  predicted_stocks: {len(records)}개 저장 완료")
    except Exception as e:
        print(f"  predicted_stocks 저장 오류: {e}")


def save_analysis_to_db(result_df):
    try:
        records = result_df.to_dict('records')
        supabase.table("stock_analysis_results").delete().neq("id", 0).execute()
        for i in range(0, len(records), 100):
            supabase.table("stock_analysis_results").insert(records[i:i+100]).execute()
        print(f"  stock_analysis_results: {len(records)}개 저장 완료")
    except Exception as e:
        print(f"  stock_analysis_results 저장 오류: {e}")

# ============================================================
# 메인 실행
# ============================================================
def main():
    total_start = time.time()

    check_environment()

    # ----------------------------------------------------------
    # PART 1: 데이터 로드 및 전처리
    # ----------------------------------------------------------
    print("=" * 60)
    print("PART 1: 데이터 로드 → 전처리")
    print("=" * 60)

    t0 = time.time()
    print("\n[1] Supabase에서 데이터 로드...")
    data = get_stock_data_from_db()
    if data is None or data.empty:
        raise ValueError("DB에서 데이터를 가져오지 못했습니다.")
    print(f"  소요시간: {time.time() - t0:.1f}초")

    print(f"\n[2] 데이터 스케일링")
    stock_scaler = MinMaxScaler()
    econ_scaler = MinMaxScaler()

    data_scaled = data.copy()
    data_scaled[TARGET_COLUMNS] = stock_scaler.fit_transform(data[TARGET_COLUMNS])
    data_scaled[ECONOMIC_FEATURES] = econ_scaler.fit_transform(data[ECONOMIC_FEATURES])

    # ----------------------------------------------------------
    # PART 2: 모델 학습
    # ----------------------------------------------------------
    print("\n" + "=" * 60)
    print("PART 2: 모델 학습")
    print("=" * 60)

    print(f"\n[3] 학습 데이터 생성")
    X_stock_train = []
    X_econ_train = []
    y_train = []

    for i in range(LOOKBACK, len(data_scaled) - FORECAST_HORIZON):
        X_stock_train.append(data_scaled[TARGET_COLUMNS].iloc[i - LOOKBACK:i].values)
        X_econ_train.append(data_scaled[ECONOMIC_FEATURES].iloc[i - LOOKBACK:i].values)
        y_train.append(data_scaled[TARGET_COLUMNS].iloc[i + FORECAST_HORIZON - 1].values)

    X_stock_train = np.array(X_stock_train)
    X_econ_train = np.array(X_econ_train)
    y_train = np.array(y_train)
    print(f"  학습 샘플: {len(y_train)}개")

    print(f"\n[4] Transformer 모델 빌드")
    stock_shape = (LOOKBACK, len(TARGET_COLUMNS))
    econ_shape = (LOOKBACK, len(ECONOMIC_FEATURES))

    model = build_transformer_with_two_inputs(
        stock_shape, econ_shape, num_heads=8, ff_dim=256, target_size=len(TARGET_COLUMNS)
    )
    model.compile(optimizer=Adam(learning_rate=LEARNING_RATE), loss='mse', metrics=['mae'])
    model.summary()

    print(f"\n[5] 학습 시작 ({EPOCHS} epochs)")
    t_train = time.time()

    history = model.fit(
        [X_stock_train, X_econ_train], y_train,
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        verbose=1
    )

    train_time = time.time() - t_train
    print(f"\n  학습 완료: {EPOCHS} epochs, 소요시간 {train_time:.1f}초 ({train_time/60:.1f}분)")

    # ----------------------------------------------------------
    # PART 3: 예측
    # ----------------------------------------------------------
    print("\n" + "=" * 60)
    print("PART 3: 전체 예측 수행")
    print("=" * 60)

    t_pred = time.time()
    X_stock_full = []
    X_econ_full = []
    for i in range(LOOKBACK, len(data_scaled)):
        X_stock_full.append(data_scaled[TARGET_COLUMNS].iloc[i - LOOKBACK:i].to_numpy())
        X_econ_full.append(data_scaled[ECONOMIC_FEATURES].iloc[i - LOOKBACK:i].to_numpy())

    X_stock_full = np.array(X_stock_full)
    X_econ_full = np.array(X_econ_full)

    predicted_prices = model.predict([X_stock_full, X_econ_full], verbose=1)
    predicted_prices_actual = stock_scaler.inverse_transform(predicted_prices)

    pred_len = len(predicted_prices_actual)
    today_dates = data['날짜'].iloc[LOOKBACK:LOOKBACK + pred_len].values
    actual_data_end = min(LOOKBACK + pred_len, len(data))
    actual_full = data[TARGET_COLUMNS].iloc[LOOKBACK:actual_data_end].values

    if actual_full.shape[0] < pred_len:
        nan_padding = np.full((pred_len - actual_full.shape[0], len(TARGET_COLUMNS)), np.nan)
        actual_full = np.vstack([actual_full, nan_padding])

    result_data = pd.DataFrame({'날짜': today_dates})
    for idx, col in enumerate(TARGET_COLUMNS):
        result_data[f'{col}_Predicted'] = predicted_prices_actual[:, idx]
        result_data[f'{col}_Actual'] = actual_full[:, idx]

    result_data['날짜'] = pd.to_datetime(result_data['날짜'], errors='coerce')
    result_data['날짜'] = result_data['날짜'].dt.strftime('%Y-%m-%d')

    print(f"  예측 완료: {pred_len}개 샘플, 소요시간 {time.time() - t_pred:.1f}초")

    # DB 저장
    print("\n  Supabase에 예측 결과 저장 중...")
    save_predictions_to_db(result_data)

    # ----------------------------------------------------------
    # PART 4: 분석 및 평가
    # ----------------------------------------------------------
    print("\n" + "=" * 60)
    print("PART 4: 분석 및 평가")
    print("=" * 60)

    print("\n[평가 결과]")
    eval_results = evaluate_predictions(result_data, TARGET_COLUMNS, FORECAST_HORIZON)
    avg_acc = eval_results['Accuracy (%)'].mean() if not eval_results.empty else 0
    print(f"  평균 정확도: {avg_acc:.2f}%")

    if not eval_results.empty:
        print(f"\n  종목별 정확도:")
        print(eval_results[['Stock', 'Accuracy (%)', 'MAPE (%)']].to_string(index=False))

    rise_results = analyze_rise_predictions(result_data, TARGET_COLUMNS)

    final_results = pd.merge(eval_results, rise_results, on='Stock', how='outer')
    final_results = final_results.sort_values(by='Rise Probability (%)', ascending=False)
    final_results['Recommendation'] = final_results.apply(generate_recommendation, axis=1)
    final_results['Analysis'] = final_results.apply(generate_analysis, axis=1)

    column_order = [
        'Stock',
        'MAE', 'MSE', 'RMSE', 'MAPE (%)', 'Accuracy (%)',
        'Last Actual Price', 'Predicted Future Price', 'Predicted Rise', 'Rise Probability (%)',
        'Recommendation', 'Analysis'
    ]
    final_results = final_results[column_order]

    print("\n  Supabase에 분석 결과 저장 중...")
    save_analysis_to_db(final_results)

    total_time = time.time() - total_start

    print("\n" + "=" * 60)
    print("FINAL REPORT")
    print("=" * 60)
    print(final_results.to_string(index=False))

    print(f"\n{'=' * 60}")
    print(f"실행 요약")
    print(f"{'=' * 60}")
    print(f"  총 소요시간: {total_time:.1f}초 ({total_time/60:.1f}분)")
    print(f"  평균 정확도: {avg_acc:.2f}%")


if __name__ == "__main__":
    main()
