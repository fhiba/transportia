import sys
import logging
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import OUTPUTS_DIR, load_gtfs_tables, TZ_BA

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


def detect_stop_arrivals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["trip_id", "vehicle_id", "stop_sequence"]).copy()
    df = df.sort_values(["vehicle_id", "trip_id", "timestamp"])

    df = df.drop_duplicates(subset=["vehicle_id", "trip_id", "stop_sequence"], keep="first")

    df["prev_seq"] = df.groupby(["vehicle_id", "trip_id"])["stop_sequence"].shift(1)
    df["prev_ts"] = df.groupby(["vehicle_id", "trip_id"])["timestamp"].shift(1)
    df["prev_stop_id"] = df.groupby(["vehicle_id", "trip_id"])["stop_id"].shift(1)
    df["prev_lat"] = df.groupby(["vehicle_id", "trip_id"])["lat"].shift(1)
    df["prev_lon"] = df.groupby(["vehicle_id", "trip_id"])["lon"].shift(1)

    arrivals = df.dropna(subset=["prev_seq"]).copy()
    arrivals = arrivals[arrivals["stop_sequence"] > arrivals["prev_seq"]]

    arrivals["travel_time_observed"] = arrivals["timestamp"] - arrivals["prev_ts"]
    arrivals = arrivals[arrivals["travel_time_observed"] > 0]

    arrivals = arrivals.rename(columns={
        "prev_seq": "arrival_seq",
        "stop_sequence": "next_seq",
        "prev_ts": "arrival_ts",
        "timestamp": "next_arrival_ts",
        "prev_stop_id": "stop_id_A",
        "stop_id": "stop_id_B",
        "prev_lat": "stop_A_lat",
        "lat": "stop_B_lat",
        "prev_lon": "stop_A_lon",
        "lon": "stop_B_lon",
    })

    return arrivals


def main():
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    stop_events_path = OUTPUTS_DIR / "stop_events.parquet"
    if not stop_events_path.exists():
        log.error("Missing %s — run 01_extract_stop_events.py first", stop_events_path)
        sys.exit(1)

    log.info("Loading stop_events.parquet...")
    se = pd.read_parquet(stop_events_path)
    log.info("Loaded %d rows", len(se))

    gtfs = load_gtfs_tables(tables=["stop_times", "trips", "routes", "stops"])
    log.info("GTFS tables loaded: %s", {k: len(v) for k, v in gtfs.items()})

    log.info("Detecting stop arrivals...")
    arrivals = detect_stop_arrivals(se)
    log.info("Detected %d stop-pair transitions", len(arrivals))
    if arrivals.empty:
        log.error("No stop arrivals detected. Check data.")
        sys.exit(1)

    seg = arrivals.copy()
    seg["trip_id"] = seg["trip_id"].str.strip()

    trips_gtfs = gtfs["trips"][["trip_id", "route_id", "direction_id"]].copy()
    trips_gtfs["trip_id"] = trips_gtfs["trip_id"].str.strip()
    trips_gtfs = trips_gtfs.rename(columns={"route_id": "route_id_gtfs", "direction_id": "direction_id_gtfs"})
    seg = seg.merge(trips_gtfs, on="trip_id", how="left")

    routes = gtfs["routes"][["route_id", "route_short_name"]].copy()
    routes["route_id"] = routes["route_id"].str.strip()
    seg = seg.merge(routes, left_on="route_id_gtfs", right_on="route_id", how="left")

    stops = gtfs["stops"][["stop_id", "stop_lat", "stop_lon"]].copy()
    stops["stop_id"] = stops["stop_id"].str.strip()
    stops_a = stops.rename(columns={"stop_lat": "gtfs_A_lat", "stop_lon": "gtfs_A_lon"})
    seg = seg.merge(stops_a, left_on="stop_id_A", right_on="stop_id", how="left")
    stops_b = stops.rename(columns={"stop_lat": "gtfs_B_lat", "stop_lon": "gtfs_B_lon"})
    seg = seg.merge(stops_b, left_on="stop_id_B", right_on="stop_id", how="left", suffixes=("", "_b"))

    seg["stop_A_lat"] = seg["gtfs_A_lat"].combine_first(seg["stop_A_lat"])
    seg["stop_A_lon"] = seg["gtfs_A_lon"].combine_first(seg["stop_A_lon"])
    seg["stop_B_lat"] = seg["gtfs_B_lat"].combine_first(seg["stop_B_lat"])
    seg["stop_B_lon"] = seg["gtfs_B_lon"].combine_first(seg["stop_B_lon"])

    seg["arrival_time"] = pd.to_datetime(seg["arrival_ts"], unit="s", utc=True)
    seg["hour"] = seg["arrival_time"].dt.tz_convert(TZ_BA).dt.hour
    seg["date"] = seg["arrival_time"].dt.tz_convert(TZ_BA).dt.date

    seg_cols = [
        "trip_id", "vehicle_id", "route_id_gtfs", "direction_id_gtfs",
        "route_short_name",
        "stop_id_A", "stop_id_B",
        "stop_A_lat", "stop_A_lon", "stop_B_lat", "stop_B_lon",
        "arrival_seq", "next_seq",
        "travel_time_observed",
        "arrival_ts", "next_arrival_ts",
        "hour", "date",
    ]
    seg_cols = [c for c in seg_cols if c in seg.columns]
    segments = seg[seg_cols].copy()

    out_seg = OUTPUTS_DIR / "segments_raw.parquet"
    segments.to_parquet(out_seg, index=False)
    log.info("Saved %s (%d rows)", out_seg, len(segments))

    # Headway
    log.info("Computing headways...")
    rid_col = "route_id_gtfs" if "route_id_gtfs" in seg.columns else "route_id"
    did_col = "direction_id_gtfs" if "direction_id_gtfs" in seg.columns else "direction_id"
    arrival_events = seg[["stop_id_A", rid_col, did_col, "arrival_ts", "route_short_name"]].copy()
    arrival_events = arrival_events.rename(columns={"stop_id_A": "stop_id", rid_col: "route_id", did_col: "direction_id"})
    arrival_events = arrival_events.dropna(subset=["stop_id", "route_id"])

    arrival_events = arrival_events.sort_values(["stop_id", "route_id", "direction_id", "arrival_ts"])
    arrival_events["prev_arrival_ts"] = arrival_events.groupby(
        ["stop_id", "route_id", "direction_id"]
    )["arrival_ts"].shift(1)
    arrival_events["headway_observed"] = arrival_events["arrival_ts"] - arrival_events["prev_arrival_ts"]
    headways = arrival_events.dropna(subset=["headway_observed"])
    headways = headways[headways["headway_observed"] > 0]

    out_hw = OUTPUTS_DIR / "headway_raw.parquet"
    headways.to_parquet(out_hw, index=False)
    log.info("Saved %s (%d rows)", out_hw, len(headways))

    # CHECKPOINT
    print("\n" + "=" * 60)
    print("CHECKPOINT — Segment Builder")
    print("=" * 60)
    print(f"Segment observations:   {len(segments):,}")
    print(f"Unique segment pairs:   {segments.groupby(['stop_id_A','stop_id_B']).ngroups:,}")
    print(f"Travel time (s):")
    tt = segments["travel_time_observed"]
    print(f"  min={tt.min():.0f}  max={tt.max():.0f}  median={tt.median():.0f}")
    print(f"  p25={tt.quantile(.25):.0f}  p75={tt.quantile(.75):.0f}  p95={tt.quantile(.95):.0f}")
    print(f"\nHeadway observations:   {len(headways):,}")
    hw = headways["headway_observed"]
    print(f"  min={hw.min():.0f}  max={hw.max():.0f}  median={hw.median():.0f}")
    print(f"  p25={hw.quantile(.25):.0f}  p75={hw.quantile(.75):.0f}  p95={hw.quantile(.95):.0f}")
    print("=" * 60)
    print("\n>>> Review the summary above. If OK, proceed to script 03.")


if __name__ == "__main__":
    main()
