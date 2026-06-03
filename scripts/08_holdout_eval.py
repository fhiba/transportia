import sys
import json
import logging
import importlib
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
import joblib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import (
    OUTPUTS_DIR, haversine_m, load_semaforos, hour_bin, day_type, TZ_BA,
)
from osrm_routing import is_osrm_available, get_route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

AMBA_LAT = (-35.0, -34.30)
AMBA_LON = (-59.00, -57.80)
CABA_LAT = (-34.75, -34.50)
CABA_LON = (-58.65, -58.30)
N_HOLDOUT = 20
TT_MIN = 5
TT_MAX = 600
GPS_GTFS_MATCH_MIN = 0.50


def build_gtfs_bbox() -> pd.DataFrame:
    try:
        from utils import GTFS_DIR
        if not GTFS_DIR.exists():
            log.warning("GTFS_DIR not found (%s) — skipping bbox validation", GTFS_DIR)
            return pd.DataFrame()
        dirs = sorted([p for p in GTFS_DIR.iterdir() if p.is_dir()])
        if not dirs:
            return pd.DataFrame()
        gtfs_dir = dirs[-1]
        log.info("Loading GTFS bbox from %s", gtfs_dir)

        routes = pd.read_csv(gtfs_dir / "routes.txt", dtype=str)
        trips = pd.read_csv(gtfs_dir / "trips.txt", dtype=str, low_memory=False)
        routes["route_id"] = routes["route_id"].str.strip()
        trips["route_id"] = trips["route_id"].str.strip()
        trips["trip_id"] = trips["trip_id"].str.strip()

        route_to_trip = trips.groupby("route_id")["trip_id"].first().to_dict()
        routes_map = routes.set_index("route_id")["route_short_name"].to_dict()

        trip_to_route = {}
        for rid, tid in route_to_trip.items():
            rsn = routes_map.get(rid)
            if rsn:
                trip_to_route[tid] = rsn

        rep_trip_ids = set(trip_to_route.keys())
        st_parts = []
        for chunk in pd.read_csv(gtfs_dir / "stop_times.txt", dtype=str, low_memory=False, chunksize=500_000):
            chunk["trip_id"] = chunk["trip_id"].str.strip()
            st_parts.append(chunk[chunk["trip_id"].isin(rep_trip_ids)])
        st = pd.concat(st_parts)

        stops = pd.read_csv(gtfs_dir / "stops.txt", dtype=str)
        stops["stop_id"] = stops["stop_id"].str.strip()
        stops["stop_lat"] = pd.to_numeric(stops["stop_lat"], errors="coerce")
        stops["stop_lon"] = pd.to_numeric(stops["stop_lon"], errors="coerce")
        st = st.merge(stops[["stop_id", "stop_lat", "stop_lon"]], on="stop_id", how="left")
        st = st.dropna(subset=["stop_lat", "stop_lon"])
        st["route_short_name"] = st["trip_id"].map(trip_to_route)
        st = st.dropna(subset=["route_short_name"])

        bbox = st.groupby("route_short_name").agg(
            gtfs_min_lat=("stop_lat", "min"),
            gtfs_max_lat=("stop_lat", "max"),
            gtfs_min_lon=("stop_lon", "min"),
            gtfs_max_lon=("stop_lon", "max"),
        ).reset_index()
        log.info("GTFS bbox computed for %d routes", len(bbox))
        return bbox
    except (FileNotFoundError, OSError) as e:
        log.warning("Could not load GTFS data (%s) — skipping bbox validation", e)
        return pd.DataFrame()


def select_holdout_routes(train_routes: set, sr: pd.DataFrame) -> list[str]:
    sr_amba = sr[
        sr["stop_A_lat"].between(*AMBA_LAT) & sr["stop_A_lon"].between(*AMBA_LON)
    ]
    route_obs = sr_amba.groupby("route_short_name").agg(
        n=("stop_A_lat", "count"),
    ).reset_index()

    # Require 90%+ of observations within CABA bounds — guarantees semaphore coverage
    caba_obs = sr_amba[
        sr_amba["stop_A_lat"].between(*CABA_LAT) & sr_amba["stop_A_lon"].between(*CABA_LON)
    ].groupby("route_short_name").size().rename("n_caba")
    route_obs = route_obs.join(caba_obs, on="route_short_name", how="left")
    route_obs["n_caba"] = route_obs["n_caba"].fillna(0)
    route_obs["pct_caba"] = route_obs["n_caba"] / route_obs["n"]

    candidates = route_obs[
        (~route_obs["route_short_name"].isin(train_routes))
        & (route_obs["pct_caba"] >= 0.90)
        & (route_obs["n"] >= 50)
    ]
    log.info("Holdout candidates (>=90%% obs in CABA): %d routes", len(candidates))

    gtfs_bbox = build_gtfs_bbox()
    if gtfs_bbox.empty:
        log.warning("No GTFS bbox available — skipping GPS/GTFS validation")
    else:
        validated = []
        for route_name in candidates["route_short_name"]:
            row = gtfs_bbox[gtfs_bbox["route_short_name"] == route_name]
            if row.empty:
                continue
            r = row.iloc[0]
            lat_span = r["gtfs_max_lat"] - r["gtfs_min_lat"]
            lon_span = r["gtfs_max_lon"] - r["gtfs_min_lon"]
            margin = 0.25
            lat_min = r["gtfs_min_lat"] - lat_span * margin
            lat_max = r["gtfs_max_lat"] + lat_span * margin
            lon_min = r["gtfs_min_lon"] - lon_span * margin
            lon_max = r["gtfs_max_lon"] + lon_span * margin

            gps_route = sr[sr["route_short_name"] == route_name]
            in_bbox = gps_route[
                gps_route["stop_A_lat"].between(lat_min, lat_max)
                & gps_route["stop_A_lon"].between(lon_min, lon_max)
            ]
            match_pct = len(in_bbox) / len(gps_route) if len(gps_route) > 0 else 0

            if match_pct >= GPS_GTFS_MATCH_MIN:
                gps_in_amba = gps_route[
                    gps_route["stop_A_lat"].between(*AMBA_LAT)
                    & gps_route["stop_A_lon"].between(*AMBA_LON)
                ]
                amba_pct = len(gps_in_amba) / len(gps_route) if len(gps_route) > 0 else 0
                if amba_pct >= 0.90:
                    validated.append(route_name)
                else:
                    log.debug("Rejected %s: AMBA coverage %.0f%%", route_name, amba_pct * 100)
            else:
                log.debug("Rejected %s: GPS/GTFS match %.0f%%", route_name, match_pct * 100)

        log.info("After GPS/GTFS validation: %d routes (rejected %d with <%.0f%% match)",
                 len(validated), len(candidates) - len(validated), GPS_GTFS_MATCH_MIN * 100)
        candidates = candidates[candidates["route_short_name"].isin(validated)]

    if len(candidates) < N_HOLDOUT:
        log.warning("Only %d candidates, using all", len(candidates))
        return candidates["route_short_name"].tolist()

    sampled = candidates.sample(N_HOLDOUT, random_state=42)
    return sampled["route_short_name"].tolist()


def compute_segment_features_for_holdout(seg: pd.DataFrame) -> pd.DataFrame:
    valid = seg.dropna(subset=["stop_A_lat", "stop_A_lon", "stop_B_lat", "stop_B_lon"]).copy()

    unique = valid.groupby(["stop_id_A", "stop_id_B"]).first().reset_index()[
        ["stop_id_A", "stop_id_B", "stop_A_lat", "stop_A_lon", "stop_B_lat", "stop_B_lon"]
    ].copy()

    unique["distance_m"] = unique.apply(
        lambda r: haversine_m(r["stop_A_lat"], r["stop_A_lon"], r["stop_B_lat"], r["stop_B_lon"]),
        axis=1,
    )
    unique["zone_lat"] = (unique["stop_A_lat"] + unique["stop_B_lat"]) / 2
    unique["zone_lon"] = (unique["stop_A_lon"] + unique["stop_B_lon"]) / 2

    semaforos = load_semaforos()
    if not semaforos.empty:
        import geopandas as gpd
        from shapely.geometry import LineString

        sem_gdf = gpd.GeoDataFrame(
            semaforos,
            geometry=gpd.points_from_xy(semaforos["longitude"], semaforos["latitude"]),
            crs="EPSG:4326",
        ).to_crs(epsg=3857)

        lines = unique.apply(
            lambda r: LineString([(r["stop_A_lon"], r["stop_A_lat"]), (r["stop_B_lon"], r["stop_B_lat"])]),
            axis=1,
        )
        seg_gdf = gpd.GeoDataFrame(unique, geometry=lines, crs="EPSG:4326").to_crs(epsg=3857)

        buffers = seg_gdf.geometry.buffer(100)
        buf_gdf = gpd.GeoDataFrame(geometry=buffers, crs="EPSG:3857")

        joined = gpd.sjoin(sem_gdf, buf_gdf, how="inner", predicate="within")

        counts = joined.groupby("index_right").size()
        op_sums = joined.groupby("index_right")["is_operational"].sum()

        unique["n_semaphores"] = 0
        unique["pct_semaphores_operational"] = np.nan
        unique.loc[counts.index, "n_semaphores"] = counts.values
        mask_gt0 = counts > 0
        unique.loc[mask_gt0.index[mask_gt0], "pct_semaphores_operational"] = (
            op_sums[mask_gt0] / counts[mask_gt0]
        ).values
    else:
        unique["n_semaphores"] = 0
        unique["pct_semaphores_operational"] = np.nan

    return unique


def build_holdout_features(sr: pd.DataFrame, holdout_routes: list[str]) -> pd.DataFrame:
    sr_holdout = sr[sr["route_short_name"].isin(holdout_routes)].copy()
    sr_holdout = sr_holdout[
        sr_holdout["stop_A_lat"].between(*AMBA_LAT) & sr_holdout["stop_A_lon"].between(*AMBA_LON)
    ]

    unique_feats = compute_segment_features_for_holdout(sr_holdout)

    sr_holdout = sr_holdout.merge(
        unique_feats,
        on=["stop_id_A", "stop_id_B", "stop_A_lat", "stop_A_lon", "stop_B_lat", "stop_B_lon"],
        how="left",
    )

    sr_holdout["hour_bin"] = sr_holdout["hour"].apply(lambda h: hour_bin(int(h)) if pd.notna(h) else None)
    if "date" in sr_holdout.columns:
        sr_holdout["day_type"] = sr_holdout["date"].apply(
            lambda d: day_type(datetime.strptime(str(d), "%Y-%m-%d")) if pd.notna(d) else None
        )
    else:
        sr_holdout["day_type"] = "weekday"

    sr_holdout = sr_holdout[
        (sr_holdout["travel_time_observed"] >= TT_MIN) & (sr_holdout["travel_time_observed"] <= TT_MAX)
    ]
    return sr_holdout


def encode_for_model(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    hour_map = {"early_morning": 0, "morning_peak": 1, "midday": 2, "evening_peak": 3, "night": 4}
    day_map = {"weekday": 0, "weekend": 1}
    if "hour_bin" in df.columns:
        df["hour_bin"] = df["hour_bin"].map(hour_map).fillna(2)
    if "day_type" in df.columns:
        df["day_type"] = df["day_type"].map(day_map).fillna(0)
    return df


def evaluate_holdout(holdout_df: pd.DataFrame, model_bundle: dict, train_metrics_path: Path):
    features = model_bundle["features"]
    model = model_bundle["model"]

    holdout_df = encode_for_model(holdout_df)

    if "pct_semaphores_operational" in holdout_df.columns:
        holdout_df["pct_semaphores_operational"] = holdout_df["pct_semaphores_operational"].fillna(0.0)
    if "n_semaphores" in holdout_df.columns:
        holdout_df["n_semaphores"] = holdout_df["n_semaphores"].fillna(0)

    available = [c for c in features if c in holdout_df.columns]
    eval_df = holdout_df.dropna(subset=available + ["travel_time_observed"]).copy()

    if eval_df.empty:
        log.error("No valid rows in holdout for evaluation")
        return None

    X = eval_df[available].values
    y = eval_df["travel_time_observed"].values
    preds = model.predict(X)

    mae = mean_absolute_error(y, preds)
    rmse = np.sqrt(mean_squared_error(y, preds))
    r2 = r2_score(y, preds)

    holdout_metrics = {"model": "XGBoost (holdout)", "MAE": mae, "RMSE": rmse, "R2": r2}

    train_metrics = None
    if train_metrics_path.exists():
        with open(train_metrics_path) as f:
            tm = json.load(f)
        train_metrics = next((r for r in tm["results"] if r["model"] == tm["best_model"]), None)

    return holdout_metrics, train_metrics, y, preds


def find_worst_holdout_route(holdout_df: pd.DataFrame) -> str:
    avg_tt = holdout_df.groupby("route_short_name")["travel_time_observed"].mean()
    return avg_tt.idxmax()


def main():
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    sf_train = pd.read_parquet(
        OUTPUTS_DIR / "segments_features.parquet", columns=["route_short_name"]
    )
    train_routes = set(sf_train["route_short_name"].unique())
    log.info("Train routes: %d", len(train_routes))

    sr_path = OUTPUTS_DIR / "segments_raw.parquet"
    if not sr_path.exists():
        log.error("Missing %s", sr_path)
        sys.exit(1)
    sr = pd.read_parquet(sr_path)
    log.info("Loaded segments_raw: %d rows", len(sr))

    holdout_routes = select_holdout_routes(train_routes, sr)
    log.info("Selected %d holdout routes: %s", len(holdout_routes), holdout_routes)

    with open(OUTPUTS_DIR / "holdout_routes.json", "w") as f:
        json.dump(holdout_routes, f, indent=2)
    log.info("Saved holdout_routes.json")

    holdout_df = build_holdout_features(sr, holdout_routes)
    log.info("Holdout features: %d rows", len(holdout_df))
    holdout_df.to_parquet(OUTPUTS_DIR / "holdout_segments.parquet", index=False)
    log.info("Saved holdout_segments.parquet")

    model_path = OUTPUTS_DIR / "model_travel_time.joblib"
    train_metrics_path = OUTPUTS_DIR / "metrics_travel_time.json"
    if not model_path.exists():
        log.error("Missing %s — train first", model_path)
        sys.exit(1)

    model_bundle = joblib.load(model_path)
    result = evaluate_holdout(holdout_df, model_bundle, train_metrics_path)

    if result is None:
        log.error("Could not evaluate holdout")
        sys.exit(1)

    holdout_metrics, train_metrics, y_true, y_pred = result

    print("\n" + "=" * 60)
    print("HOLDOUT EVALUATION — Rutas no vistas en entrenamiento")
    print("=" * 60)
    print(f"Holdout routes: {len(holdout_routes)}")
    print(f"Holdout segments evaluated: {len(y_true):,}")
    print()
    print(f"{'Set':<20} {'MAE':>10} {'RMSE':>10} {'R²':>10}")
    print("-" * 52)
    if train_metrics:
        print(f"{'Train test':<20} {train_metrics['MAE']:>10.1f} {train_metrics['RMSE']:>10.1f} {train_metrics['R2']:>10.3f}")
    print(f"{'Holdout (20 rutas)':<20} {holdout_metrics['MAE']:>10.1f} {holdout_metrics['RMSE']:>10.1f} {holdout_metrics['R2']:>10.3f}")
    if train_metrics:
        diff_mae = holdout_metrics["MAE"] - train_metrics["MAE"]
        diff_rmse = holdout_metrics["RMSE"] - train_metrics["RMSE"]
        diff_r2 = holdout_metrics["R2"] - train_metrics["R2"]
        print(f"\nDegradación: MAE {diff_mae:+.1f}s, RMSE {diff_rmse:+.1f}s, R² {diff_r2:+.3f}")
    print("=" * 60)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    max_val = 400
    axes[0].scatter(y_true, y_pred, alpha=0.3, s=5)
    axes[0].plot([0, max_val], [0, max_val], "r--", label="Identidad")
    axes[0].set_xlabel("Real (s)")
    axes[0].set_ylabel("Predicho (s)")
    axes[0].set_title("Holdout: Real vs Predicho (rutas no vistas)")
    axes[0].set_xlim(0, max_val)
    axes[0].set_ylim(0, max_val)
    axes[0].legend()
    axes[0].grid(True, alpha=0.3, color="gray", linestyle="--")

    residuals = y_pred - y_true
    axes[1].hist(residuals, bins=50, edgecolor="black")
    axes[1].axvline(0, color="r", linestyle="--")
    axes[1].set_xlabel("Residuo (s)")
    axes[1].set_ylabel("Frecuencia")
    axes[1].set_title("Holdout: Distribución de residuos")
    axes[1].grid(True, alpha=0.3, color="gray", linestyle="--")
    plt.tight_layout()
    fig_path = OUTPUTS_DIR / "holdout_diagnostics.png"
    plt.savefig(fig_path, dpi=150)
    plt.close()
    log.info("Saved %s", fig_path)

    worst_route = find_worst_holdout_route(holdout_df)
    worst_avg = holdout_df[holdout_df["route_short_name"] == worst_route]["travel_time_observed"].mean()
    log.info("Worst holdout route: %s (avg TT=%.0fs)", worst_route, worst_avg)

    print(f"\nPeor ruta holdout: {worst_route} (avg TT={worst_avg:.0f}s)")
    print(">>> Usar esta ruta en el simulador del notebook (celda holdout)")

    holdout_metrics_path = OUTPUTS_DIR / "metrics_holdout.json"
    with open(holdout_metrics_path, "w") as f:
        json.dump({
            "holdout_routes": holdout_routes,
            "holdout_metrics": holdout_metrics,
            "train_metrics": train_metrics,
            "worst_route": worst_route,
            "n_holdout_segments": len(y_true),
        }, f, indent=2)
    log.info("Saved %s", holdout_metrics_path)


if __name__ == "__main__":
    main()
