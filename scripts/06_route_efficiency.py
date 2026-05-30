import sys
import logging
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import OUTPUTS_DIR, load_gtfs_tables, TZ_BA

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


def compute_route_scores(stop_events_path: Path, gtfs: dict) -> pd.DataFrame:
    import geopandas as gpd
    from shapely.geometry import LineString

    se = pd.read_parquet(stop_events_path, columns=["trip_id", "lat", "lon", "timestamp"])
    se = se.dropna(subset=["lat", "lon", "trip_id"])

    shapes = gtfs["shapes"].copy()
    shapes = shapes.sort_values(["shape_id", "shape_pt_sequence"])

    trips = gtfs["trips"][["trip_id", "route_id", "direction_id", "shape_id"]].copy()
    trips["trip_id"] = trips["trip_id"].str.strip()
    se["trip_id"] = se["trip_id"].str.strip()

    se_trips = se.merge(trips, on="trip_id", how="inner")
    log.info("Matched %d / %d pings with GTFS trips", len(se_trips), len(se))

    if se_trips.empty:
        return pd.DataFrame()

    shape_lines = {}
    for shape_id, grp in shapes.groupby("shape_id"):
        pts = list(zip(grp["shape_pt_lon"].values, grp["shape_pt_lat"].values))
        if len(pts) >= 2:
            shape_lines[shape_id] = LineString(pts)

    log.info("Loaded %d shapes", len(shape_lines))

    route_scores = []
    grouped = se_trips.groupby(["route_id", "direction_id"])
    total = len(grouped)
    log.info("Computing scores for %d route+direction groups...", total)

    for i, ((route_id, direction_id), grp) in enumerate(grouped):
        if i % 100 == 0:
            log.info("  Route %d/%d", i, total)

        grp = grp.sort_values("timestamp")

        shape_ids = grp["shape_id"].dropna().unique()
        if len(shape_ids) == 0 or shape_ids[0] not in shape_lines:
            continue

        planned_line = shape_lines[shape_ids[0]]

        sample = grp.dropna(subset=["lat", "lon"])
        if len(sample) > 500:
            sample = sample.sample(500, random_state=42)

        if len(sample) < 5:
            continue

        obs_pts = list(zip(sample["lon"].values, sample["lat"].values))

        try:
            obs_gdf = gpd.GeoSeries([LineString(obs_pts)], crs="EPSG:4326").to_crs(epsg=3857)
            plan_gdf = gpd.GeoSeries([planned_line], crs="EPSG:4326").to_crs(epsg=3857)
        except Exception:
            continue

        obs_proj = obs_gdf.iloc[0]
        plan_proj = plan_gdf.iloc[0]

        hausdorff_m = obs_proj.hausdorff_distance(plan_proj)

        from shapely.geometry import Point
        obs_points_proj = [gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(epsg=3857).iloc[0]
                           for lon, lat in obs_pts]
        point_dists = [plan_proj.distance(p) for p in obs_points_proj]
        avg_point_dist = np.mean(point_dists)

        observed_travel = grp["timestamp"].max() - grp["timestamp"].min()
        n_stops_est = len(sample) // 3
        planned_travel = n_stops_est * 120 if n_stops_est > 0 else observed_travel

        temporal_dev = (
            abs(observed_travel - planned_travel) / planned_travel * 100
            if planned_travel > 0 else 0
        )

        efficiency_score = avg_point_dist * 0.5 + temporal_dev * 10

        route_scores.append({
            "route_id": route_id,
            "direction_id": direction_id,
            "spatial_deviation_m": avg_point_dist,
            "hausdorff_m": hausdorff_m,
            "temporal_deviation_pct": temporal_dev,
            "efficiency_score": efficiency_score,
            "n_pings": len(grp),
            "n_trips": grp["trip_id"].nunique(),
        })

    return pd.DataFrame(route_scores)


def main():
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    stop_events_path = OUTPUTS_DIR / "stop_events.parquet"
    if not stop_events_path.exists():
        log.error("Missing %s — run 01_extract_stop_events.py first", stop_events_path)
        sys.exit(1)

    log.info("Loading GTFS tables...")
    gtfs = load_gtfs_tables(tables=["shapes", "trips", "routes"])

    log.info("Computing route efficiency scores...")
    scores = compute_route_scores(stop_events_path, gtfs)

    if scores.empty:
        log.error("No route scores computed.")
        sys.exit(1)

    if "routes" in gtfs:
        routes = gtfs["routes"][["route_id", "route_short_name"]].copy()
        routes["route_id"] = routes["route_id"].str.strip()
        scores["route_id"] = scores["route_id"].str.strip()
        scores = scores.merge(routes, on="route_id", how="left")

    scores = scores.sort_values("efficiency_score", ascending=False)

    out_path = OUTPUTS_DIR / "route_scores.parquet"
    scores.to_parquet(out_path, index=False)
    log.info("Saved %s (%d routes)", out_path, len(scores))

    print("\n" + "=" * 60)
    print("CHECKPOINT — Route Efficiency")
    print("=" * 60)
    print(f"Routes scored: {len(scores)}")
    print(f"\nTop 10 WORST routes:")
    worst = scores.head(10)
    cols = ["route_short_name", "route_id", "spatial_deviation_m", "temporal_deviation_pct", "efficiency_score"]
    print(worst[[c for c in cols if c in worst.columns]].to_string(index=False))
    print(f"\nTop 10 BEST routes:")
    best = scores.tail(10).iloc[::-1]
    print(best[[c for c in cols if c in best.columns]].to_string(index=False))
    print("=" * 60)
    print("\n>>> Review the ranking above. If OK, proceed to script 07.")


if __name__ == "__main__":
    main()
