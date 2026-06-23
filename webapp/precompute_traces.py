"""Precomputa polylines observadas por línea desde stop_events.parquet.

Para cada route_short_name:
  1. Encuentra el best_trip (mismo criterio que build_route_data).
  2. Filtra los pings GPS de ese trip en stop_events.parquet.
  3. Limpia outliers (saltos > MAX_JUMP_M) y submuestrea (min sep MIN_STEP_M).
  4. Guarda la polyline en outputs/observed_traces/<route>.json.

El engine carga estos JSONs y los usa como current_geometry — sigue las calles
reales que tomó el colectivo, sin depender de GTFS shapes (que pueden ser
parciales o de variantes distintas).

Uso:
    python webapp/precompute_traces.py            # todas las líneas
    python webapp/precompute_traces.py 185C 39A   # solo las pasadas
    python webapp/precompute_traces.py --force    # sobreescribe
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "webapp"))

from utils import OUTPUTS_DIR, haversine_m  # noqa: E402

TRACES_DIR = OUTPUTS_DIR / "observed_traces"

# Limpieza de pings GPS
MAX_JUMP_M = 2000.0     # descartar glitches (>2km entre pings consecutivos)
MIN_STEP_M = 100.0      # submuestrear: un punto cada ~100m (densidad uniforme sin forzar)
MAX_POINTS = 300        # si después de limpiar quedan más, submuestrear uniformemente
MIN_POINTS = 10         # si quedan menos, descartar la trace
MIN_PINGS_PER_TRIP = 50  # mínimo de pings para considerar un trip válido
POINTS_PER_SEGMENT = 8   # cada segmento coloreado cubre ~N puntos consecutivos de la trace
TRIP_GAP_S = 600.0       # gap > 10min entre pings = viaje distinto (mismo trip_id se reutiliza)


def _safe_route_name(route: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(route))


def _split_into_trips(df: pd.DataFrame, gap_s: float = TRIP_GAP_S) -> list[pd.DataFrame]:
    """Parte los pings en viajes individuales por gaps temporales.

    Un ``trip_id`` GTFS identifica un viaje "ideal" que se ejecuta muchas veces
    (diferentes vehículos, diferentes días). Si los pings se ordenan por timestamp
    y hay un gap > gap_s, es porque cambió el vehículo físico — cortar ahí.
    """
    df = df.dropna(subset=["lat", "lon", "timestamp"]).sort_values("timestamp")
    df = df.drop_duplicates(subset=["lat", "lon", "timestamp"])
    if len(df) < 2:
        return []

    ts = df["timestamp"].values.astype(float)
    deltas = ts[1:] - ts[:-1]
    cut_indices = [0] + [i + 1 for i, d in enumerate(deltas) if d > gap_s] + [len(df)]

    trips = []
    for k in range(len(cut_indices) - 1):
        start, end = cut_indices[k], cut_indices[k + 1]
        if end - start >= 5:
            trips.append(df.iloc[start:end])
    return trips


def _clean_pings(df: pd.DataFrame) -> tuple[list[list[float]], list[float]]:
    """Ordena por timestamp, saca outliers y submuestrea.

    Devuelve (points, timestamps_seg) con la polyline limpia y sus tiempos.
    """
    df = df.dropna(subset=["lat", "lon", "timestamp"]).sort_values("timestamp")
    df = df.drop_duplicates(subset=["lat", "lon", "timestamp"])
    if len(df) < 2:
        return [], []

    lats = df["lat"].values
    lons = df["lon"].values
    ts = df["timestamp"].values.astype(float)

    out_pts: list[tuple[float, float]] = [(float(lats[0]), float(lons[0]))]
    out_ts: list[float] = [float(ts[0])]

    for i in range(1, len(lats)):
        prev_lat, prev_lon = out_pts[-1]
        lat, lon = float(lats[i]), float(lons[i])
        d = haversine_m(prev_lat, prev_lon, lat, lon)
        if d > MAX_JUMP_M:
            continue  # glitch extremo
        if d < MIN_STEP_M:
            # actualizar timestamp del último punto (el bus estuvo acá por más tiempo)
            out_ts[-1] = float(ts[i])
            continue
        out_pts.append((lat, lon))
        out_ts.append(float(ts[i]))

    # Submuestrear si hay demasiados puntos
    if len(out_pts) > MAX_POINTS:
        idx = [round(i * (len(out_pts) - 1) / (MAX_POINTS - 1)) for i in range(MAX_POINTS)]
        seen = set()
        kept = [i for i in idx if not (i in seen or seen.add(i))]
        out_pts = [out_pts[i] for i in kept]
        out_ts = [out_ts[i] for i in kept]

    points = [[p[0], p[1]] for p in out_pts]
    return points, out_ts


def _build_segments(
    points: list[list[float]], ts: list[float], pts_per_seg: int = POINTS_PER_SEGMENT
) -> list[dict]:
    """Reparte la trace en chunks de ``pts_per_seg`` puntos consecutivos.

    Cada segmento incluye ``points`` (sub-polyline), ``speed_kmh``, ``dt_s``, ``dist_m``.
    Usar chunks por cantidad de puntos (no por distancia) porque la trace observada
    puede incluir loops/múltiples viajes con densidad variable.
    """
    if len(points) < 2 or len(ts) != len(points):
        return []

    segments: list[dict] = []
    i = 0
    while i < len(points) - 1:
        end_i = min(i + pts_per_seg, len(points))
        seg_pts = points[i:end_i]
        if len(seg_pts) < 2:
            break

        # distancia acumulada dentro del segmento
        seg_d = sum(
            haversine_m(seg_pts[k][0], seg_pts[k][1], seg_pts[k + 1][0], seg_pts[k + 1][1])
            for k in range(len(seg_pts) - 1)
        )
        dt = ts[end_i - 1] - ts[i]
        if dt <= 0:
            i = end_i
            continue
        speed = seg_d / dt  # m/s
        segments.append({
            "points": seg_pts,
            "speed_kmh": round(speed * 3.6, 1),
            "dt_s": round(dt, 1),
            "dist_m": round(seg_d, 1),
        })
        i = end_i - 1  # solape de 1 punto para continuidad visual

    return segments


def _build_best_trips_map(se: pd.DataFrame) -> dict[str, tuple[str, str]]:
    """route_short_name → (trip_id, vehicle_id) con MÁS PINGS en stop_events.

    Un trip_id GTFS se ejecuta múltiples veces (diferentes vehículos). Para una
    trace coherente, filtramos por un único vehicle_id que tenga más pings.
    """
    counts = se.dropna(subset=["route_short_name", "trip_id", "vehicle_id", "lat", "lon"]).groupby(
        ["route_short_name", "trip_id", "vehicle_id"]
    ).size().reset_index(name="n")
    counts = counts[counts["n"] >= MIN_PINGS_PER_TRIP]
    if counts.empty:
        return {}
    idx = counts.groupby("route_short_name")["n"].idxmax()
    best = counts.loc[idx]
    return {row["route_short_name"]: (row["trip_id"], row["vehicle_id"]) for _, row in best.iterrows()}


def main() -> None:
    TRACES_DIR.mkdir(parents=True, exist_ok=True)

    sf_path = OUTPUTS_DIR / "segments_features.parquet"
    se_path = OUTPUTS_DIR / "stop_events.parquet"
    if not sf_path.exists() or not se_path.exists():
        print(f"ERROR: faltan parquets en {OUTPUTS_DIR}/")
        sys.exit(1)

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    force = "--force" in sys.argv

    print("Cargando segments_features.parquet para lista de rutas...")
    sf = pd.read_parquet(sf_path, columns=["route_short_name"])
    sf_routes = set(sf["route_short_name"].dropna().unique())
    print(f"  {len(sf_routes)} rutas en segments_features")

    print("Cargando stop_events.parquet (puede tardar)...")
    t0 = time.monotonic()
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from utils import load_gtfs_tables  # noqa: E402
    gtfs = load_gtfs_tables(tables=["routes", "trips"])
    route_map = dict(zip(
        gtfs["routes"]["route_id"].str.strip(),
        gtfs["routes"]["route_short_name"]
    ))
    trip_route = dict(zip(
        gtfs["trips"]["trip_id"].str.strip(),
        gtfs["trips"]["route_id"].str.strip()
    ))

    # vehicle_id es clave: un trip_id GTFS identifica un viaje "ideal" que se ejecuta
    # múltiples veces por diferentes vehículos. Para armar la trace de un solo viaje
    # físico, necesitamos un único vehicle_id.
    se = pd.read_parquet(
        se_path, columns=["trip_id", "vehicle_id", "lat", "lon", "timestamp"]
    )
    print(f"  {len(se):,} pings en {time.monotonic()-t0:.1f}s")

    # Mapear trip_id → route_short_name vía GTFS
    print("Mapeando trip_id → route_short_name vía GTFS...")
    t0 = time.monotonic()
    se["_route_id"] = se["trip_id"].map(trip_route)
    se["route_short_name"] = se["_route_id"].map(route_map)
    n_mapped = se["route_short_name"].notna().sum()
    print(f"  {n_mapped:,} pings con route_short_name en {time.monotonic()-t0:.1f}s")

    # Filtrar solo rutas presentes en segments_features
    se = se[se["route_short_name"].isin(sf_routes)]
    se = se.dropna(subset=["vehicle_id"])
    print(f"  {len(se):,} pings tras filtrar a {len(sf_routes)} rutas")

    best_trips = _build_best_trips_map(se)
    if args:
        best_trips = {r: t for r, t in best_trips.items() if r in args}
    print(f"\nGenerando traces para {len(best_trips)} líneas...")

    n_ok = n_skip = n_err = 0
    t_total = time.monotonic()
    for i, (route, (trip_id, vehicle_id)) in enumerate(sorted(best_trips.items()), 1):
        out_path = TRACES_DIR / f"{_safe_route_name(route)}.json"
        if out_path.exists() and not force:
            n_skip += 1
            print(f"[{i:>3}/{len(best_trips)}] {route:>10}  SKIP")
            continue

        # Filtrar por trip_id Y vehicle_id (un solo viaje físico)
        pings = se[(se["trip_id"] == trip_id) & (se["vehicle_id"] == vehicle_id)]
        if len(pings) < MIN_PINGS_PER_TRIP:
            n_err += 1
            print(f"[{i:>3}/{len(best_trips)}] {route:>10}  ERROR (solo {len(pings)} pings)")
            continue

        points, ts = _clean_pings(pings)
        if len(points) < MIN_POINTS:
            n_err += 1
            print(f"[{i:>3}/{len(best_trips)}] {route:>10}  ERROR ({len(points)} pts tras limpieza)")
            continue

        segments = _build_segments(points, ts)

        payload = {
            "route": str(route),
            "trip_id": str(trip_id),
            "n_pings": int(len(pings)),
            "n_points": len(points),
            "n_segments": len(segments),
            "points": points,
            "segments": segments,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        n_ok += 1
        print(f"[{i:>3}/{len(best_trips)}] {route:>10}  OK  {len(pings):>4} pings (1 viaje) → {len(points)} pts · {len(segments)} segs")

    dt = time.monotonic() - t_total
    print(f"\nListo en {dt:.1f}s · {n_ok} nuevas · {n_skip} skip · {n_err} errores")
    print(f"Output: {TRACES_DIR}/")


if __name__ == "__main__":
    main()
