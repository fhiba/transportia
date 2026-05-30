import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import VP_DIR, OUTPUTS_DIR, fname_to_dt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

BLOCK_B_START = "20260424_140000"
CHUNK_SIZE = 200


def extract_entity_fields(entity: dict, file_dt) -> dict | None:
    v = entity.get("_vehicle")
    if v is None:
        return None
    trip = v.get("_trip") or {}
    pos = v.get("_position") or {}
    veh = v.get("_vehicle") or {}
    return {
        "trip_id": trip.get("_trip_id"),
        "route_id": trip.get("_route_id"),
        "direction_id": trip.get("_direction_id"),
        "vehicle_id": veh.get("_id"),
        "vehicle_label": veh.get("_label"),
        "lat": pos.get("_latitude"),
        "lon": pos.get("_longitude"),
        "speed": pos.get("_speed"),
        "stop_id": v.get("_stop_id"),
        "stop_sequence": v.get("_current_stop_sequence"),
        "current_status": v.get("_current_status"),
        "occupancy_status": v.get("_occupancy_status"),
        "congestion_level": v.get("_congestion_level"),
        "timestamp": v.get("_timestamp"),
        "_file_dt": file_dt,
    }


def main():
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(VP_DIR.glob("*.json"))
    log.info("Total files: %d (no stride — full resolution)", len(files))

    files_b = [f for f in files if f.stem >= BLOCK_B_START]
    log.info("Block B files (>= %s): %d", BLOCK_B_START, len(files_b))

    chunk_paths = []
    rows = []
    errors = 0
    total_rows = 0

    for i, fp in enumerate(files_b):
        if i % 200 == 0:
            log.info("Processing file %d/%d: %s", i, len(files_b), fp.name)
        try:
            with open(fp) as f:
                data = json.load(f)
            file_dt = fname_to_dt(fp.name)
            entities = data.get("_entity", [])
            for entity in entities:
                row = extract_entity_fields(entity, file_dt)
                if row is not None:
                    rows.append(row)
        except Exception as e:
            errors += 1
            log.warning("Error parsing %s: %s", fp.name, e)
            continue

        if len(rows) >= CHUNK_SIZE * 9500:
            chunk_df = pd.DataFrame(rows)
            for col in ("lat", "lon", "speed", "timestamp"):
                if col in chunk_df.columns:
                    chunk_df[col] = pd.to_numeric(chunk_df[col], errors="coerce")
            for col in ("stop_sequence", "current_status", "occupancy_status", "congestion_level", "direction_id"):
                if col in chunk_df.columns:
                    chunk_df[col] = pd.to_numeric(chunk_df[col], errors="coerce").astype("Int64")
            cp = OUTPUTS_DIR / f"_chunk_{len(chunk_paths)}.parquet"
            chunk_df.to_parquet(cp, index=False)
            total_rows += len(chunk_df)
            log.info("Flushed chunk %d (%d rows, total %d)", len(chunk_paths), len(chunk_df), total_rows)
            chunk_paths.append(cp)
            rows = []
            del chunk_df

    if rows:
        chunk_df = pd.DataFrame(rows)
        for col in ("lat", "lon", "speed", "timestamp"):
            if col in chunk_df.columns:
                chunk_df[col] = pd.to_numeric(chunk_df[col], errors="coerce")
        for col in ("stop_sequence", "current_status", "occupancy_status", "congestion_level", "direction_id"):
            if col in chunk_df.columns:
                chunk_df[col] = pd.to_numeric(chunk_df[col], errors="coerce").astype("Int64")
        cp = OUTPUTS_DIR / f"_chunk_{len(chunk_paths)}.parquet"
        chunk_df.to_parquet(cp, index=False)
        total_rows += len(chunk_df)
        chunk_paths.append(cp)
        log.info("Flushed final chunk (%d rows, total %d)", len(chunk_df), total_rows)

    log.info("Merging %d chunks...", len(chunk_paths))
    df = pd.concat((pd.read_parquet(p) for p in chunk_paths), ignore_index=True)
    for p in chunk_paths:
        p.unlink()
    log.info("Combined: %d rows", len(df))

    out_path = OUTPUTS_DIR / "stop_events.parquet"
    df.to_parquet(out_path, index=False)
    log.info("Saved %s (%.1f MB)", out_path, out_path.stat().st_size / 1e6)

    print("\n" + "=" * 60)
    print("CHECKPOINT — Stop Events Extraction")
    print("=" * 60)
    print(f"Total rows:             {len(df):,}")
    print(f"Rows with stop_id:      {df['stop_id'].notna().sum():,}")
    print(f"Rows with trip_id:      {df['trip_id'].notna().sum():,}")
    print(f"Unique trips:           {df['trip_id'].nunique():,}")
    print(f"Unique vehicles:        {df['vehicle_id'].nunique():,}")
    print(f"Unique routes:          {df['route_id'].nunique():,}")
    print(f"Date range:             {df['_file_dt'].min()} → {df['_file_dt'].max()}")
    print("=" * 60)
    print("\n>>> Review the summary above. If OK, proceed to script 02.")


if __name__ == "__main__":
    main()
