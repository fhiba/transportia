import sys
import logging
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import (
    OUTPUTS_DIR,
    haversine_m,
    load_gtfs_tables,
    load_frequencies,
    load_semaforos,
    hour_bin,
    day_type,
    TZ_BA,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


def compute_unique_segment_features(seg: pd.DataFrame, semaforos: pd.DataFrame) -> pd.DataFrame:
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

    unique["segment_curvature"] = 1.0
    return unique


def main():
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    seg_path = OUTPUTS_DIR / "segments_raw.parquet"
    hw_path = OUTPUTS_DIR / "headway_raw.parquet"

    if not seg_path.exists():
        log.error("Missing %s — run 02_build_segments.py first", seg_path)
        sys.exit(1)

    log.info("Loading segments_raw.parquet...")
    seg = pd.read_parquet(seg_path)
    log.info("Loaded %d segment observations", len(seg))

    hw = None
    if hw_path.exists():
        hw = pd.read_parquet(hw_path)
        log.info("Loaded %d headway observations", len(hw))

    log.info("Computing unique segment features (distance, semaphores)...")
    semaforos = load_semaforos()
    log.info("Loaded %d semaphores", len(semaforos))

    unique_feats = compute_unique_segment_features(seg, semaforos)
    log.info("Computed features for %d unique segments", len(unique_feats))

    seg = seg.merge(
        unique_feats,
        on=["stop_id_A", "stop_id_B", "stop_A_lat", "stop_A_lon", "stop_B_lat", "stop_B_lon"],
        how="left",
    )

    seg["hour_bin"] = seg["hour"].apply(lambda h: hour_bin(int(h)) if pd.notna(h) else None)
    if "date" in seg.columns:
        seg["day_type"] = seg["date"].apply(
            lambda d: day_type(datetime.strptime(str(d), "%Y-%m-%d")) if pd.notna(d) else None
        )
    else:
        seg["day_type"] = "weekday"

    log.info("Computing avg_speed_zone_hour (mean travel_time per zone/hour)...")
    speed_map = seg.groupby(["zone_lat", "zone_lon", "hour_bin"])["travel_time_observed"].mean()
    seg["avg_speed_zone_hour"] = seg.set_index(["zone_lat", "zone_lon", "hour_bin"]).index.map(
        speed_map.get
    )

    n_before = len(seg)
    seg = seg[(seg["travel_time_observed"] >= 5) & (seg["travel_time_observed"] <= 900)]
    log.info("Filtered travel_time outliers (>900s = GPS artifacts/breaks): %d → %d (removed %d)", n_before, len(seg), n_before - len(seg))

    out_seg = OUTPUTS_DIR / "segments_features.parquet"
    seg.to_parquet(out_seg, index=False)
    log.info("Saved %s (%d rows)", out_seg, len(seg))

    if hw is not None:
        log.info("Loading GTFS tables for headway features...")
        gtfs = load_gtfs_tables(tables=["stops"])

        hw = hw.copy()
        stops = gtfs["stops"][["stop_id", "stop_lat", "stop_lon"]].copy()
        stops["stop_id"] = stops["stop_id"].str.strip()
        hw["stop_id"] = hw["stop_id"].str.strip()
        hw = hw.merge(stops, on="stop_id", how="left")

        if "arrival_ts" in hw.columns:
            ts = pd.to_datetime(hw["arrival_ts"], unit="s", utc=True)
            hw["hour"] = ts.dt.tz_convert(TZ_BA).dt.hour
        hw["hour_bin"] = hw["hour"].apply(lambda h: hour_bin(int(h)) if pd.notna(h) else None)

        if "route_short_name" in hw.columns and "route_id" in hw.columns:
            n_veh = seg.groupby(["route_id_gtfs", "hour_bin"])["vehicle_id"].nunique()
            n_veh = n_veh.reset_index().rename(columns={"route_id_gtfs": "route_id"})
            hw["route_id"] = hw["route_id"].astype(str)
            n_veh["route_id"] = n_veh["route_id"].astype(str)
            hw = hw.merge(n_veh, on=["route_id", "hour_bin"], how="left")
            hw = hw.rename(columns={"vehicle_id": "n_vehicles_active"})

        gtfs_freq = load_frequencies()
        if not gtfs_freq.empty:
            log.info("Loading GTFS frequency headway (avg per route_short_name)...")
            gtfs_all = load_gtfs_tables(tables=["trips", "routes"])
            tr = gtfs_all["trips"][["trip_id", "route_id"]].copy()
            tr["trip_id"] = tr["trip_id"].str.strip()
            tr["route_id"] = tr["route_id"].str.strip()
            ro = gtfs_all["routes"][["route_id", "route_short_name"]].copy()
            ro["route_id"] = ro["route_id"].str.strip()
            tr_ro = tr.merge(ro, on="route_id", how="left")

            freq_gtfs_dir = load_frequencies.__defaults__[0] if load_frequencies.__defaults__ else None
            from utils import GTFS_FREQ_DIR, _latest_gtfs_freq_dir
            gtfs_freq_dir = _latest_gtfs_freq_dir()
            freq_trips = gtfs_freq_dir / "trips.txt"
            freq_routes_file = gtfs_freq_dir / "routes.txt"

            if freq_trips.exists() and freq_routes_file.exists():
                freq_tr = pd.read_csv(freq_trips, dtype=str)
                freq_ro = pd.read_csv(freq_routes_file, dtype=str)
                freq_tr["trip_id"] = freq_tr["trip_id"].str.strip()
                freq_tr["route_id"] = freq_tr["route_id"].str.strip()
                freq_ro["route_id"] = freq_ro["route_id"].str.strip()

                gtfs_freq["trip_id"] = gtfs_freq["trip_id"].str.strip()
                freq_with_route = gtfs_freq.merge(freq_tr[["trip_id", "route_id"]], on="trip_id", how="left")
                freq_with_route = freq_with_route.merge(freq_ro[["route_id", "route_short_name"]], on="route_id", how="left")

                avg_hw = freq_with_route.groupby("route_short_name")["headway_secs"].mean().reset_index()
                avg_hw = avg_hw.rename(columns={"headway_secs": "headway_programado"})
                avg_hw["route_short_name"] = avg_hw["route_short_name"].str.strip()

                if "route_short_name" in hw.columns:
                    hw["route_short_name"] = hw["route_short_name"].astype(str).str.strip()
                    hw = hw.merge(avg_hw, on="route_short_name", how="left")
                    log.info("Headway programado matched: %d / %d rows", hw["headway_programado"].notna().sum(), len(hw))

        n_before_hw = len(hw)
        hw = hw[(hw["headway_observed"] >= 30) & (hw["headway_observed"] <= 7200)]
        log.info("Filtered headway outliers: %d → %d", n_before_hw, len(hw))

        out_hw = OUTPUTS_DIR / "headway_features.parquet"
        hw.to_parquet(out_hw, index=False)
        log.info("Saved %s (%d rows)", out_hw, len(hw))

    # CHECKPOINT
    print("\n" + "=" * 60)
    print("CHECKPOINT — Feature Engineering")
    print("=" * 60)

    feature_cols = ["distance_m", "zone_lat", "zone_lon", "hour_bin", "day_type",
                    "n_semaphores", "pct_semaphores_operational", "segment_curvature",
                    "avg_speed_zone_hour"]
    existing = [c for c in feature_cols if c in seg.columns]

    print("\n--- Segment Features ---")
    print(f"Rows: {len(seg):,}")
    print(f"\nNull counts:")
    for c in existing:
        print(f"  {c}: {seg[c].isna().sum():,}")
    print(f"\nCorrelation with travel_time_observed:")
    for c in existing:
        if seg[c].dtype in [np.float64, np.int64, np.float32, np.int32]:
            corr = seg[c].corr(seg["travel_time_observed"])
            print(f"  {c}: {corr:.3f}")

    if hw is not None:
        print("\n--- Headway Features ---")
        print(f"Rows: {len(hw):,}")
        print(f"Columns: {list(hw.columns)}")
        if "headway_programado" in hw.columns:
            valid = hw.dropna(subset=["headway_programado"])
            print(f"Rows with GTFS headway: {len(valid):,}")
        if "n_vehicles_active" in hw.columns:
            print(f"n_vehicles_active non-null: {hw['n_vehicles_active'].notna().sum():,}")

    print("=" * 60)
    print("\n>>> Review the summary above. If OK, proceed to script 04.")


if __name__ == "__main__":
    main()
