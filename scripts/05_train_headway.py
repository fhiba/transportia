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
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
import joblib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import OUTPUTS_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


def encode_categoricals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "hour_bin" in df.columns:
        hour_map = {"early_morning": 0, "morning_peak": 1, "midday": 2, "evening_peak": 3, "night": 4}
        df["hour_bin_enc"] = df["hour_bin"].map(hour_map).fillna(2)
    else:
        df["hour_bin_enc"] = 2
    if "day_type" in df.columns:
        day_map = {"weekday": 0, "weekend": 1}
        df["day_type_enc"] = df["day_type"].map(day_map).fillna(0)
    else:
        df["day_type_enc"] = 0
    return df


def main():
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    hw_path = OUTPUTS_DIR / "headway_features.parquet"
    if not hw_path.exists():
        log.error("Missing %s — run 03_feature_engineering.py first", hw_path)
        sys.exit(1)

    log.info("Loading headway_features.parquet...")
    df = pd.read_parquet(hw_path)
    log.info("Loaded %d rows", len(df))

    df = encode_categoricals(df)

    feature_candidates = ["stop_lat", "stop_lon", "hour_bin_enc", "day_type_enc", "n_vehicles_active", "headway_programado"]
    existing_features = [c for c in feature_candidates if c in df.columns]

    df_model = df.dropna(subset=existing_features + ["headway_observed"]).copy()
    log.info("Rows after dropping nulls: %d", len(df_model))

    if len(df_model) < 100:
        log.error("Too few rows for training: %d", len(df_model))
        sys.exit(1)

    if len(df_model) > 300_000:
        log.info("Sampling 300K rows for tractable training...")
        df_model = df_model.sample(300_000, random_state=42)

    X = df_model[existing_features].values
    y = df_model["headway_observed"].values

    strat_col = "route_short_name" if "route_short_name" in df_model.columns else None
    strat = None
    if strat_col:
        df_model["_strat"] = df_model[strat_col].fillna("UNK") + "_" + df_model["hour_bin_enc"].astype(str)
        strat = df_model["_strat"]
        min_class = strat.value_counts()
        rare = min_class[min_class < 2].index
        strat = strat.replace(rare, "_rare")

    try:
        X_train, X_test, y_train, y_test, idx_train, idx_test = train_test_split(
            X, y, np.arange(len(X)), test_size=0.2, random_state=42, stratify=strat
        )
    except ValueError:
        log.warning("Stratification failed — random split")
        X_train, X_test, y_train, y_test, idx_train, idx_test = train_test_split(
            X, y, np.arange(len(X)), test_size=0.2, random_state=42
        )

    log.info("Train: %d | Test: %d", len(X_train), len(X_test))

    log.info("Training Random Forest...")
    rf_params = {"n_estimators": [100], "max_depth": [20]}
    rf_model = GridSearchCV(
        RandomForestRegressor(random_state=42, n_jobs=-1),
        rf_params, cv=3, scoring="neg_root_mean_squared_error", n_jobs=-1, verbose=1,
    )
    rf_model.fit(X_train, y_train)
    log.info("RF best params: %s", rf_model.best_params_)

    log.info("Training GradientBoosting...")
    gb_params = {"n_estimators": [100], "max_depth": [4], "learning_rate": [0.1]}
    gb_model = GridSearchCV(
        GradientBoostingRegressor(random_state=42),
        gb_params, cv=3, scoring="neg_root_mean_squared_error", n_jobs=-1, verbose=1,
    )
    gb_model.fit(X_train, y_train)
    log.info("GB best params: %s", gb_model.best_params_)

    pred_rf = rf_model.best_estimator_.predict(X_test)
    pred_gb = gb_model.best_estimator_.predict(X_test)

    def metrics(y_true, y_pred, name):
        mae = mean_absolute_error(y_true, y_pred)
        rmse = np.sqrt(mean_squared_error(y_true, y_pred))
        r2 = r2_score(y_true, y_pred)
        return {"model": name, "MAE": mae, "RMSE": rmse, "R2": r2}

    results = [
        metrics(y_test, pred_rf, "RandomForest"),
        metrics(y_test, pred_gb, "GradientBoosting"),
    ]

    if "headway_programado" in df_model.columns:
        hw_prog_test = df_model.iloc[idx_test]["headway_programado"].values
        valid = ~np.isnan(hw_prog_test)
        if valid.sum() > 0:
            results.append(metrics(y_test[valid], hw_prog_test[valid], "GTFS programado"))

    for r in results:
        log.info("%(model)s: MAE=%(MAE).1f  RMSE=%(RMSE).1f  R2=%(R2).3f", r)

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    rf_imp = rf_model.best_estimator_.feature_importances_
    order_rf = np.argsort(rf_imp)[::-1][:15]
    axes[0].barh(range(len(order_rf)), rf_imp[order_rf])
    axes[0].set_yticks(range(len(order_rf)))
    axes[0].set_yticklabels([existing_features[i] for i in order_rf])
    axes[0].set_title("Random Forest Feature Importance")
    axes[0].invert_yaxis()

    gb_imp = gb_model.best_estimator_.feature_importances_
    order_gb = np.argsort(gb_imp)[::-1][:15]
    axes[1].barh(range(len(order_gb)), gb_imp[order_gb])
    axes[1].set_yticks(range(len(order_gb)))
    axes[1].set_yticklabels([existing_features[i] for i in order_gb])
    axes[1].set_title("GradientBoosting Feature Importance")
    axes[1].invert_yaxis()
    plt.tight_layout()

    fig_path = OUTPUTS_DIR / "feature_importance_headway.png"
    plt.savefig(fig_path, dpi=150)
    plt.close()
    log.info("Saved %s", fig_path)

    best_idx = np.argmin([r["RMSE"] for r in results[:2]])
    best_model = rf_model.best_estimator_ if best_idx == 0 else gb_model.best_estimator_
    best_name = results[best_idx]["model"]

    model_path = OUTPUTS_DIR / "model_headway.joblib"
    joblib.dump({"model": best_model, "features": existing_features, "model_name": best_name}, model_path)
    log.info("Saved %s (best: %s)", model_path, best_name)

    metrics_path = OUTPUTS_DIR / "metrics_headway.json"
    with open(metrics_path, "w") as f:
        json.dump({"results": results, "best_model": best_name, "features": existing_features}, f, indent=2)
    log.info("Saved %s", metrics_path)

    print("\n" + "=" * 60)
    print("CHECKPOINT — Headway Model")
    print("=" * 60)
    print(f"{'Model':<20} {'MAE':>10} {'RMSE':>10} {'R²':>10}")
    print("-" * 52)
    for r in results:
        print(f"{r['model']:<20} {r['MAE']:>10.1f} {r['RMSE']:>10.1f} {r['R2']:>10.3f}")
    print(f"\nBest model: {best_name}")
    print("=" * 60)
    print("\n>>> Review metrics above. If OK, proceed to script 06.")


if __name__ == "__main__":
    main()
