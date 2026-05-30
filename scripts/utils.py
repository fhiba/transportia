from pathlib import Path
from datetime import datetime, timezone, timedelta
from math import radians, sin, cos, sqrt, atan2

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

TZ_BA = timezone(timedelta(hours=-3))

STRIDE = 4

AMBA_BOUNDS = {
    "lat_min": -35.0,
    "lat_max": -34.30,
    "lon_min": -59.00,
    "lon_max": -57.80,
}

VP_DIR = DATA_DIR / "colectivos" / "vehicle_positions"
GTFS_DIR = DATA_DIR / "colectivos" / "feed_gtfs"
GTFS_FREQ_DIR = DATA_DIR / "colectivos" / "feed_gtfs_frequency"
SEMAFOROS_DIR = DATA_DIR / "transito" / "v1" / "semaforos"

VP_SIMPLE_DIR = DATA_DIR / "colectivos" / "vehicle_positions_simple"


def fname_to_dt(fname: str) -> datetime:
    stem = Path(fname).stem
    return datetime.strptime(stem, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def load_gtfs_tables(gtfs_dir: Path = None, tables=None):
    if gtfs_dir is None:
        gtfs_dir = _latest_gtfs_dir()
    if tables is None:
        tables = ["stops", "trips", "routes", "stop_times", "shapes"]
    result = {}
    for name in tables:
        path = gtfs_dir / f"{name}.txt"
        if path.exists():
            result[name] = pd.read_csv(path, dtype=str, low_memory=False)
            if name == "stop_times" and "stop_sequence" in result[name].columns:
                result[name]["stop_sequence"] = pd.to_numeric(
                    result[name]["stop_sequence"], errors="coerce"
                ).astype("Int64")
            if name == "stop_times" and "shape_dist_traveled" in result[name].columns:
                result[name]["shape_dist_traveled"] = pd.to_numeric(
                    result[name]["shape_dist_traveled"], errors="coerce"
                )
            if name == "shapes":
                for col in ("shape_pt_lat", "shape_pt_lon", "shape_dist_traveled"):
                    if col in result[name].columns:
                        result[name][col] = pd.to_numeric(
                            result[name][col], errors="coerce"
                        )
                if "shape_pt_sequence" in result[name].columns:
                    result[name]["shape_pt_sequence"] = pd.to_numeric(
                        result[name]["shape_pt_sequence"], errors="coerce"
                    ).astype("Int64")
            if name == "stops":
                for col in ("stop_lat", "stop_lon"):
                    if col in result[name].columns:
                        result[name][col] = pd.to_numeric(
                            result[name][col], errors="coerce"
                        )
            if name == "trips" and "direction_id" in result[name].columns:
                result[name]["direction_id"] = pd.to_numeric(
                    result[name]["direction_id"], errors="coerce"
                ).astype("Int64")
    return result


def load_frequencies(gtfs_freq_dir: Path = None):
    if gtfs_freq_dir is None:
        gtfs_freq_dir = _latest_gtfs_freq_dir()
    path = gtfs_freq_dir / "frequencies.txt"
    if path.exists():
        df = pd.read_csv(path, dtype=str)
        df["headway_secs"] = pd.to_numeric(df["headway_secs"], errors="coerce")
        return df
    return pd.DataFrame()


def load_semaforos():
    import json

    semaforos = []
    if not SEMAFOROS_DIR.exists():
        return pd.DataFrame()
    for fp in sorted(SEMAFOROS_DIR.glob("*.json")):
        with open(fp) as f:
            data = json.load(f)
        items = data.get("list", data if isinstance(data, list) else [])
        for s in items:
            semaforos.append(
                {
                    "code": s.get("code"),
                    "name": s.get("name"),
                    "status": s.get("status"),
                    "latitude": s.get("latitude"),
                    "longitude": s.get("longitude"),
                }
            )
        break
    df = pd.DataFrame(semaforos)
    for col in ("latitude", "longitude"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["is_operational"] = df["status"].str.lower().str.contains("conectado", na=False)
    return df


def hour_bin(hour: int) -> str:
    if 5 <= hour < 7:
        return "early_morning"
    elif 7 <= hour < 10:
        return "morning_peak"
    elif 10 <= hour < 16:
        return "midday"
    elif 16 <= hour < 20:
        return "evening_peak"
    else:
        return "night"


def day_type(dt: datetime) -> str:
    return "weekend" if dt.weekday() >= 5 else "weekday"


def _latest_gtfs_dir() -> Path:
    zips = sorted(GTFS_DIR.glob("*"))
    dirs = [p for p in zips if p.is_dir()]
    if not dirs:
        raise FileNotFoundError(f"No GTFS data found in {GTFS_DIR}")
    return dirs[-1]


def _latest_gtfs_freq_dir() -> Path:
    zips = sorted(GTFS_FREQ_DIR.glob("*"))
    dirs = [p for p in zips if p.is_dir()]
    if not dirs:
        raise FileNotFoundError(f"No GTFS frequency data found in {GTFS_FREQ_DIR}")
    return dirs[-1]
