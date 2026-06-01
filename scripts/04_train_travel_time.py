import sys
import json
import logging
from pathlib import Path

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.model_selection import GridSearchCV, train_test_split
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
import xgboost as xgb
import joblib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import OUTPUTS_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

FEATURE_COLS = [
    "distance_m", "zone_lat", "zone_lon", "hour_bin",
    "day_type", "route_enc",
    "n_semaphores", "pct_semaphores_operational",
]


def encode_categoricals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "hour_bin" in df.columns:
        hour_map = {"early_morning": 0, "morning_peak": 1, "midday": 2, "evening_peak": 3, "night": 4}
        df["hour_bin"] = df["hour_bin"].map(hour_map).fillna(2)
    if "day_type" in df.columns:
        day_map = {"weekday": 0, "weekend": 1}
        df["day_type"] = df["day_type"].map(day_map).fillna(0)
    if "route_short_name" in df.columns:
        freq = df["route_short_name"].value_counts()
        df["route_enc"] = df["route_short_name"].map(freq).fillna(0).astype(int)
    else:
        df["route_enc"] = 0
    return df


def main():
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    seg_path = OUTPUTS_DIR / "segments_features.parquet"
    if not seg_path.exists():
        log.error("Missing %s — run 03_feature_engineering.py first", seg_path)
        sys.exit(1)

    log.info("Loading segments_features.parquet...")
    df = pd.read_parquet(seg_path)
    log.info("Loaded %d rows", len(df))

    existing_features = [c for c in FEATURE_COLS if c in df.columns]
    df = encode_categoricals(df)
    df_model = df.dropna(subset=existing_features + ["travel_time_observed"]).copy()
    log.info("Rows after dropping nulls: %d", len(df_model))

    if len(df_model) > 300_000:
        log.info("Sampling 300K rows for tractable training...")
        df_model = df_model.sample(300_000, random_state=42)

    if len(df_model) < 100:
        log.error("Too few rows for training: %d", len(df_model))
        sys.exit(1)

    X = df_model[existing_features].values
    y = df_model["travel_time_observed"].values

    strat_col = "route_short_name" if "route_short_name" in df_model.columns else None
    if strat_col:
        df_model["_strat"] = df_model[strat_col].fillna("UNK") + "_" + df_model["hour_bin"].astype(str)
        strat = df_model["_strat"]
    else:
        strat = None

    if strat is not None:
        min_class = strat.value_counts()
        rare = min_class[min_class < 2].index
        strat = strat.replace(rare, "_rare")
    try:
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=strat
        )
    except ValueError:
        log.warning("Stratification failed — falling back to random split")
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )

    log.info("Train: %d | Test: %d", len(X_train), len(X_test))

    log.info("Training XGBoost with GridSearch...")
    xgb_params = {
        "n_estimators": [100, 200],
        "max_depth": [4, 6, 8],
        "learning_rate": [0.05, 0.1],
    }
    xgb_model = GridSearchCV(
        xgb.XGBRegressor(random_state=42, verbosity=0),
        xgb_params,
        cv=3,
        scoring="neg_root_mean_squared_error",
        n_jobs=-1,
        verbose=1,
    )
    xgb_model.fit(X_train, y_train)
    log.info("XGB best params: %s", xgb_model.best_params_)

    log.info("Training Random Forest with GridSearch...")
    rf_params = {
        "n_estimators": [100, 200, 300],
        "max_depth": [10, 20, 30, None],
        "min_samples_leaf": [1, 5],
    }
    rf_model = GridSearchCV(
        RandomForestRegressor(random_state=42, n_jobs=-1),
        rf_params,
        cv=3,
        scoring="neg_root_mean_squared_error",
        n_jobs=-1,
        verbose=1,
    )
    rf_model.fit(X_train, y_train)
    log.info("RF best params: %s", rf_model.best_params_)

    pred_xgb = xgb_model.best_estimator_.predict(X_test)
    pred_rf = rf_model.best_estimator_.predict(X_test)

    def metrics(y_true, y_pred, name):
        mae = mean_absolute_error(y_true, y_pred)
        rmse = np.sqrt(mean_squared_error(y_true, y_pred))
        r2 = r2_score(y_true, y_pred)
        return {"model": name, "MAE": mae, "RMSE": rmse, "R2": r2}

    results = [
        metrics(y_test, pred_xgb, "XGBoost"),
        metrics(y_test, pred_rf, "RandomForest"),
        metrics(y_test, np.full_like(y_test, y_train.mean()), "Baseline (mean)"),
    ]

    if "distance_m" in df_model.columns:
        bins = [0, 200, 500, 1000, 2000, 5000, float("inf")]
        train_copy = df_model.iloc[:len(X_train)].copy()
        test_copy = df_model.iloc[len(X_train):len(X_train)+len(X_test)].copy()
        if len(test_copy) == len(y_test):
            train_copy["_db"] = pd.cut(train_copy["distance_m"], bins=bins)
            bucket_means = train_copy.groupby("_db", observed=True)["travel_time_observed"].mean().to_dict()
            test_db = pd.cut(test_copy["distance_m"], bins=bins)
            baseline_bucket = np.array([bucket_means.get(b, y_train.mean()) for b in test_db])
            results.append(metrics(y_test, baseline_bucket, "Baseline (dist bucket)"))

    for r in results:
        log.info("%(model)s: MAE=%(MAE).1f  RMSE=%(RMSE).1f  R2=%(R2).3f", r)

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    for ax, model_obj, title_prefix in [
        (axes[0], xgb_model, "XGBoost"),
        (axes[1], rf_model, "Random Forest"),
    ]:
        imp = model_obj.best_estimator_.feature_importances_
        order = np.argsort(imp)[::-1][:15]
        ax.barh(range(len(order)), imp[order])
        ax.set_yticks(range(len(order)))
        ax.set_yticklabels([existing_features[i] for i in order])
        ax.set_title(f"{title_prefix} Feature Importance")
        ax.invert_yaxis()
        ax.grid(True, alpha=0.3, color="gray", linestyle="--")

    plt.tight_layout()
    fig_path = OUTPUTS_DIR / "feature_importance_travel_time.png"
    plt.savefig(fig_path, dpi=150)
    plt.close()
    log.info("Saved %s", fig_path)

    best_idx = np.argmin([r["RMSE"] for r in results[:2]])
    best_model = xgb_model.best_estimator_ if best_idx == 0 else rf_model.best_estimator_
    best_name = results[best_idx]["model"]

    model_path = OUTPUTS_DIR / "model_travel_time.joblib"
    joblib.dump({"model": best_model, "features": existing_features, "model_name": best_name}, model_path)
    log.info("Saved %s (best: %s)", model_path, best_name)

    metrics_path = OUTPUTS_DIR / "metrics_travel_time.json"
    with open(metrics_path, "w") as f:
        json.dump({"results": results, "best_model": best_name, "features": existing_features}, f, indent=2)
    log.info("Saved %s", metrics_path)

    print("\n" + "=" * 60)
    print("CHECKPOINT — Travel Time Model")
    print("=" * 60)
    print(f"{'Model':<20} {'MAE':>10} {'RMSE':>10} {'R²':>10}")
    print("-" * 52)
    for r in results:
        print(f"{r['model']:<20} {r['MAE']:>10.1f} {r['RMSE']:>10.1f} {r['R2']:>10.3f}")
    print(f"\nBest model: {best_name}")
    imp = best_model.feature_importances_
    top_idx = np.argsort(imp)[::-1][:5]
    print(f"Top features: {[existing_features[i] for i in top_idx]}")
    print("=" * 60)
    print("\n>>> Review metrics above. If OK, proceed to script 05.")


if __name__ == "__main__":
    main()
