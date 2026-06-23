"""Capa de servicio del front: lista líneas disponibles y corre el simulador por línea.

Reutiliza la lógica pura de ``scripts/07_simulate_route.py`` (grafo de paradas, detección
de cuellos de botella y propuestas). El grafo se arma SIEMPRE con haversine (cero llamadas
a OSRM en el loop). La geometría de la RUTA ACTUAL viene directo de GTFS shapes (trazado
oficial, sin diagonales). OSRM se reserva para las propuestas, con presupuesto dedicado
por propuesta.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# scripts/ debe estar en sys.path: 07 hace `from utils import ...` y `from osrm_routing import ...`
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from utils import OUTPUTS_DIR, haversine_m, load_gtfs_tables  # noqa: E402
from osrm_routing import (  # noqa: E402
    DEFAULT_OSRM_URL,
    PUBLIC_OSRM_URL,
    get_route,
    get_route_waypoints,
    is_osrm_available,
    match_route,
)


def _load_sim_module():
    """Carga scripts/07_simulate_route.py (nombre con dígitos → no importable directo)."""
    path = SCRIPTS_DIR / "07_simulate_route.py"
    spec = importlib.util.spec_from_file_location("sim07", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SIM = _load_sim_module()
build_route_data = _SIM.build_route_data
build_bus_graph = _SIM.build_bus_graph
precompute_graph_inputs = _SIM.precompute_graph_inputs
find_bottleneck_zones = _SIM.find_bottleneck_zones
propose_alternatives = _SIM.propose_alternatives

# --- Criterio "línea disponible" (≥90% del recorrido en CABA). Igual a 08_holdout_eval.py ---
CABA_LAT = (-34.75, -34.50)
CABA_LON = (-58.65, -58.30)
MIN_CABA_PCT = 0.90

# Geometría / rendimiento
SF_COLUMNS = [
    "route_short_name", "trip_id", "arrival_seq", "next_seq",
    "stop_id_A", "stop_id_B", "stop_A_lat", "stop_A_lon",
    "stop_B_lat", "stop_B_lon", "travel_time_observed", "distance_m",
]
PROPOSAL_OSRM_BUDGET = 2          # llamadas OSRM dedicadas por propuesta (no compartido)
OSRM_TIMEOUT = 6.0                # seg por llamada
WAYPOINT_CHUNK = 80               # OSRM público limita waypoints por request
DEDUPE_EPS_M = 15                 # paradas casi-iguales consecutivas
DETOUR_MAX_FACTOR = 1.6           # si la alternativa rodea > 1.6× la original, se descarta
OSRM_AVAILABILITY_TTL = 60.0      # re-checkear OSRM cada 60s, no por request

CACHE_DIR = OUTPUTS_DIR / "cache"
TRACES_DIR = OUTPUTS_DIR / "observed_traces"


# ---------------------------------------------------------------------------
# Carga única de datos (a nivel módulo — no se relee por request)
# ---------------------------------------------------------------------------
def _try_load():
    sf_path = OUTPUTS_DIR / "segments_features.parquet"
    sc_path = OUTPUTS_DIR / "route_scores.parquet"
    missing = [p.name for p in (sf_path, sc_path) if not p.exists()]
    if missing:
        return None, None, f"Faltan en outputs/: {', '.join(missing)}. Copialos desde la otra compu."
    try:
        sf = pd.read_parquet(sf_path, columns=SF_COLUMNS)
        sc = pd.read_parquet(sc_path)
        return sf, sc, None
    except Exception as exc:  # noqa: BLE001
        return None, None, f"No se pudieron leer los parquets: {exc}"


_SF, _SCORES, _LOAD_ERROR = _try_load()


# ---------------------------------------------------------------------------
# Precompute global: graph_inputs (no depende de la ruta) + shapes GTFS
# ---------------------------------------------------------------------------
def _build_precomputed():
    """Carga una sola vez los agregados de sf y los shapes GTFS oficiales."""
    if _SF is None:
        return None, None, None
    graph_inputs = precompute_graph_inputs(_SF)
    shape_lookup, trip_shape = _load_gtfs_shapes()
    return graph_inputs, shape_lookup, trip_shape


def _load_gtfs_shapes():
    """Carga trips.txt (trip_id → shape_id) y shapes.txt (shape_id → [(lat, lon), ...]).

    Devuelve (shape_lookup, trip_shape). Si no hay GTFS disponible, devuelve (None, None)
    y el simulador cae al snap OSRM/stops como antes.
    """
    try:
        gtfs = load_gtfs_tables(tables=["shapes", "trips"])
    except Exception:  # noqa: BLE001
        return None, None

    if "shapes" not in gtfs or "trips" not in gtfs:
        return None, None

    shapes_df = gtfs["shapes"].sort_values(["shape_id", "shape_pt_sequence"])
    shape_lookup: dict[str, list[list[float]]] = {}
    for shape_id, grp in shapes_df.groupby("shape_id"):
        pts = [
            [float(lat), float(lon)]
            for lat, lon in zip(grp["shape_pt_lat"].values, grp["shape_pt_lon"].values)
            if pd.notna(lat) and pd.notna(lon)
        ]
        if len(pts) >= 2:
            shape_lookup[str(shape_id)] = pts

    trips_df = gtfs["trips"]
    if "shape_id" not in trips_df.columns or "trip_id" not in trips_df.columns:
        return shape_lookup, {}
    trip_shape = {
        str(tid): str(sid)
        for tid, sid in zip(trips_df["trip_id"].values, trips_df["shape_id"].values)
        if pd.notna(sid)
    }
    return shape_lookup, trip_shape


def _load_observed_traces() -> dict[str, dict]:
    """Carga traces observadas (GPS) desde outputs/observed_traces/*.json.

    Cada archivo tiene ``{route, points, segments: [{points, speed_kmh, ...}]}``.
    Si el directorio no existe o está vacío, devuelve {} y el simulador cae a GTFS shape / stops.
    """
    traces: dict[str, dict] = {}
    if not TRACES_DIR.exists():
        return traces
    for fp in TRACES_DIR.glob("*.json"):
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
            pts = data.get("points") or []
            route = data.get("route") or fp.stem
            if len(pts) >= 2:
                traces[str(route)] = data
        except (OSError, json.JSONDecodeError):
            continue
    return traces


_GRAPH_INPUTS, _SHAPE_LOOKUP, _TRIP_SHAPE = _build_precomputed()
_OBSERVED_TRACES = _load_observed_traces()


# Coherencia espacial: descarta líneas donde la trace observada y las paradas
# están en corredores distintos (variante distinta del mismo route_short_name).
MAX_TRACE_STOP_GAP_KM = 3.0


def _compute_valid_lines() -> set[str]:
    """Set de route_short_name cuyo recorrido, paradas y heatmap son coherentes.

    Criterio (excluye si NO pasa cualquiera):
      - Trace observada (si existe) a >3km del centroide de paradas ⇒ variante
        geográfica distinta del mismo route_short_name.
      - Heatmap vacío tras ``build_route_data`` ⇒ datos pobres (travel_time o
        distance mayoritariamente NaN).
    """
    if _SF is None:
        return set()

    valid: set[str] = set()
    for route, grp in _SF[_SF["stop_A_lat"].notna()].groupby("route_short_name"):
        if grp.empty:
            continue
        best_trip = grp.groupby("trip_id").size().sort_values(ascending=False).index[0]
        trip = grp[grp["trip_id"] == best_trip]
        # cond 1: centroide de paradas
        s_c = (float(trip["stop_A_lat"].mean()), float(trip["stop_A_lon"].mean()))
        trace = _OBSERVED_TRACES.get(str(route))
        if trace and len(trace.get("points", [])) >= 2:
            obs_pts = trace["points"]
            obs_c = (
                sum(p[0] for p in obs_pts) / len(obs_pts),
                sum(p[1] for p in obs_pts) / len(obs_pts),
            )
            if haversine_m(obs_c[0], obs_c[1], s_c[0], s_c[1]) / 1000 > MAX_TRACE_STOP_GAP_KM:
                continue  # variante geográfica distinta
        # cond 2: heatmap viable (travel_time + distance con datos)
        _, seg_stats = build_route_data(grp)
        if seg_stats.empty:
            continue
        if seg_stats["med_tt"].isna().all() or (seg_stats["med_tt"].fillna(0) <= 0).all():
            continue  # sin tiempos observados ⇒ heatmap vacío
        valid.add(str(route))
    return valid


_VALID_LINES = _compute_valid_lines()


def data_ready() -> bool:
    return _SF is not None


def load_error() -> str | None:
    return _LOAD_ERROR


# ---------------------------------------------------------------------------
# Cache de resultados (runtime + disco)
# ---------------------------------------------------------------------------
_RESULT_CACHE: dict[str, dict] = {}


def _cache_path(route: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(route))
    return CACHE_DIR / f"{safe}.json"


def _load_cached(route: str) -> dict | None:
    """Trae el JSON precomputado desde outputs/cache/<route>.json si existe."""
    path = _cache_path(route)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Líneas disponibles
# ---------------------------------------------------------------------------
def list_available_lines() -> list[dict]:
    if _SF is None:
        return []

    df = _SF.dropna(subset=["stop_A_lat", "stop_A_lon", "route_short_name"])
    grp = df.groupby("route_short_name")
    n_total = grp.size().rename("n")
    in_caba = (
        df["stop_A_lat"].between(*CABA_LAT) & df["stop_A_lon"].between(*CABA_LON)
    )
    n_caba = df[in_caba].groupby("route_short_name").size().rename("n_caba")

    summary = pd.concat([n_total, n_caba], axis=1).fillna({"n_caba": 0})
    summary["pct_caba"] = summary["n_caba"] / summary["n"]
    summary["n_stops"] = grp["stop_id_A"].nunique()

    available = summary[summary["pct_caba"] >= MIN_CABA_PCT].copy()

    # efficiency_score por línea (la peor dirección)
    scores = _SCORES.copy()
    if "route_short_name" in scores.columns and "efficiency_score" in scores.columns:
        eff = scores.groupby("route_short_name")["efficiency_score"].max()
        available = available.join(eff, how="inner")  # solo líneas con score
    else:
        available["efficiency_score"] = 0.0

    available = available.sort_values("efficiency_score", ascending=False)

    out = []
    for route, row in available.iterrows():
        if _VALID_LINES and str(route) not in _VALID_LINES:
            continue  # línea incoherente (variante distinta / datos pobres)
        out.append({
            "route_short_name": str(route),
            "efficiency_score": float(row.get("efficiency_score", 0.0)),
            "pct_caba": round(float(row["pct_caba"]) * 100, 1),
            "n_stops": int(row["n_stops"]),
        })
    return out


# ---------------------------------------------------------------------------
# Helpers de geometría
# ---------------------------------------------------------------------------
def _dedupe(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Saca puntos consecutivos casi iguales (lat, lon)."""
    out: list[tuple[float, float]] = []
    for lat, lon in coords:
        if lat is None or lon is None or (isinstance(lat, float) and math.isnan(lat)):
            continue
        if out and haversine_m(out[-1][0], out[-1][1], lat, lon) < DEDUPE_EPS_M:
            continue
        out.append((float(lat), float(lon)))
    return out


def _order_intermediates(a, b, mids):
    """Ordena paradas intermedias por proyección sobre A→B y descarta retrocesos."""
    ax, ay = a[1], a[0]  # x=lon, y=lat
    bx, by = b[1], b[0]
    vx, vy = bx - ax, by - ay
    denom = vx * vx + vy * vy
    if denom == 0:
        return []
    scored = []
    for m in mids:
        mx, my = m[1], m[0]
        t = ((mx - ax) * vx + (my - ay) * vy) / denom
        if -0.02 <= t <= 1.05:
            scored.append((t, m))
    scored.sort(key=lambda s: s[0])
    ordered, last_t = [], -1.0
    for t, m in scored:
        if t > last_t:           # estrictamente creciente: avanza hacia B
            ordered.append(m)
            last_t = t
    return ordered


def _path_len_m(coords: list[tuple[float, float]]) -> float:
    return sum(
        haversine_m(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
        for i in range(len(coords) - 1)
    )


def _pick_osrm_url() -> str | None:
    """Devuelve la URL OSRM disponible (local > público > None). Cacheado con TTL.

    Antes se llamaba a la red en CADA request (hasta 6s muertos si no había OSRM).
    Ahora solo re-probamos cada OSRM_AVAILABILITY_TTL segundos.
    """
    now = time.monotonic()
    cached = _pick_osrm_url._cached  # type: ignore[attr-defined]
    if cached is not None and (now - cached["ts"]) < OSRM_AVAILABILITY_TTL:
        return cached["url"]

    url = None
    if is_osrm_available(DEFAULT_OSRM_URL):
        url = DEFAULT_OSRM_URL
    elif is_osrm_available(PUBLIC_OSRM_URL):
        url = PUBLIC_OSRM_URL

    _pick_osrm_url._cached = {"url": url, "ts": now}  # type: ignore[attr-defined]
    return url


_pick_osrm_url._cached = None  # type: ignore[attr-defined]


def _snap(coords_latlon, osrm_url, budget) -> list[list[float]]:
    """Snapea una secuencia (lat,lon) a las calles vía OSRM (chunked). Devuelve [[lat,lon],...].

    Si no hay OSRM o se agota el presupuesto, devuelve la línea recta entre las paradas.
    ``budget`` es una lista de un elemento [restantes] que se decrementa.
    """
    coords = _dedupe(coords_latlon)
    if len(coords) < 2:
        return [[c[0], c[1]] for c in coords]

    if osrm_url is None or budget[0] <= 0:
        return [[c[0], c[1]] for c in coords]

    geometry: list[list[float]] = []
    i = 0
    while i < len(coords) - 1:
        chunk = coords[i:i + WAYPOINT_CHUNK]
        wpts = [(lon, lat) for lat, lon in chunk]  # OSRM espera (lon, lat)
        snapped = None
        if budget[0] > 0:
            budget[0] -= 1
            # /match (map-matching): pega el rastro a las calles SIN forzar el paso por
            # cada parada → evita los rulos/"dientes de peine" en avenidas anchas.
            res = match_route(wpts, osrm_url=osrm_url, timeout=OSRM_TIMEOUT)
            # Fallback a /route si el match no devuelve geometría utilizable.
            if not (res and res.get("geometry")):
                res = get_route_waypoints(wpts, osrm_url=osrm_url, timeout=OSRM_TIMEOUT)
            if res and res.get("geometry"):
                snapped = [[pt[1], pt[0]] for pt in res["geometry"]]  # [lon,lat]→[lat,lon]
        if snapped is None:
            snapped = [[lat, lon] for lat, lon in chunk]  # fallback recto
        if geometry and snapped and geometry[-1] == snapped[0]:
            snapped = snapped[1:]
        geometry.extend(snapped)
        i += WAYPOINT_CHUNK - 1  # solape de 1 punto
    return geometry


# ---------------------------------------------------------------------------
# Helpers de geometría de la ruta actual
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Helpers de geometría de la ruta actual
# ---------------------------------------------------------------------------
SHAPE_MATCH_MAX_M = 300.0  # si el shape GTFS pasa a >300m de las paradas, se rechaza


def _shape_stop_distance_m(shape_pts: list[list[float]], stops_seq) -> float:
    """Distancia media (m) desde cada parada al punto más cercano del shape.
    Mientras más chico, mejor calza el shape con las paradas observadas."""
    if not shape_pts or len(stops_seq) == 0:
        return float("inf")
    # muestrear el shape (max ~80 puntos) para no hacer O(N*M) gigante
    step = max(1, len(shape_pts) // 80)
    sampled = shape_pts[::step]

    total = 0.0
    n = 0
    for _, stop in stops_seq.iterrows():
        s_lat, s_lon = float(stop["lat"]), float(stop["lon"])
        # haversine a cada punto del shape, quedarse con el mínimo
        min_d = min(haversine_m(s_lat, s_lon, p[0], p[1]) for p in sampled)
        total += min_d
        n += 1
    return total / n if n else float("inf")


def _resolve_current_shape(
    route_short_name: str, r_data, stops_seq, osrm_url
) -> tuple[list[list[float]], str]:
    """Elige la polilínea para la ruta actual.

    Prioridades:
      1. Trace observada (GPS) — sigue las calles reales del colectivo.
         Precomputada por ``webapp/precompute_traces.py``.
      2. GTFS shape oficial (fallback).
      3. Recto entre paradas (fallback final).

    La mezcla fina (qué paradas caen cerca del shape vs lejos) la hace
    ``_shape_to_segments``, que produce slices del shape o stops rectos según
    corresponda, así que podemos tolerar shapes que cubran solo parte del recorrido.
    """
    # 1. Trace observada (GPS)
    if route_short_name in _OBSERVED_TRACES:
        trace = _OBSERVED_TRACES[route_short_name]
        return trace.get("points", []), "observed"

    # 2. GTFS shape más común entre los trips observados
    if _SHAPE_LOOKUP and _TRIP_SHAPE:
        from collections import Counter
        shape_counter: Counter = Counter()
        for trip_id in r_data["trip_id"].unique():
            sid = _TRIP_SHAPE.get(str(trip_id))
            if sid and sid in _SHAPE_LOOKUP:
                shape_counter[sid] += 1

        if shape_counter:
            best_sid, _ = shape_counter.most_common(1)[0]
            return _SHAPE_LOOKUP[best_sid], "gtfs"

    # 3. Stops rectos
    current_latlon = [(float(r["lat"]), float(r["lon"])) for _, r in stops_seq.iterrows()]
    return [[lat, lon] for lat, lon in current_latlon], "stops"


def _intensity_from_observed_segments(segments: list[dict]) -> list[dict]:
    """Convierte segments crudos de la trace observada al formato que usa el frontend.

    Cada segment del JSON tiene ``{points, speed_kmh, dt_s, dist_m}``.
    Calcula ``intensity`` relativo a la velocidad media de la trace.
    """
    if not segments:
        return []
    speeds = [s.get("speed_kmh", 0) or 0 for s in segments]
    valid = [v for v in speeds if v > 0]
    if not valid:
        return []
    mean_speed_kmh = sum(valid) / len(valid)

    out: list[dict] = []
    for s in segments:
        speed = s.get("speed_kmh") or 0
        # intensity ∈ [0, 1]: 0 = a velocidad media o mejor, 1 = detenido
        if mean_speed_kmh > 0 and speed > 0:
            intensity = max(0.0, min(1.0, (mean_speed_kmh - speed) / mean_speed_kmh))
        else:
            intensity = 0.0
        out.append({
            "points": s["points"],
            "intensity": round(intensity, 3),
            "speed_kmh": s.get("speed_kmh"),
            "med_tt": s.get("dt_s"),
        })
    return out


def _shape_to_segments(
    shape_pts: list[list[float]], stops_seq, seg_stats
) -> list[dict]:
    """Rebanar el shape GTFS por las posiciones de las paradas y pegarle velocidad.

    Devuelve una lista de segmentos: ``{points, intensity, speed_kmh, med_tt}``.
    El frontend dibuja cada uno como polyline con color interpolado por intensity.

    ``intensity`` ∈ [0, 1]: 0 = velocidad media o mejor o sin datos, 1 = detenido.

    Si una parada queda lejos del shape (>300m), el segmento usa stops rectos.
    Esto permite mezclar GTFS shape (donde calza) con stops (donde no) dentro de
    la misma línea, para tolerar shapes parciales o ramales distintos.
    """
    if not shape_pts or len(stops_seq) < 2:
        return []
    if "arrival_seq" not in stops_seq.columns or "arrival_seq" not in seg_stats.columns:
        return []

    total_tt = float(seg_stats["med_tt"].sum())
    total_dist = float(seg_stats["med_dist"].sum())
    has_speed = total_tt > 0 and total_dist > 0
    mean_speed = total_dist / total_tt if has_speed else 0.0

    # LEFT join: todos los stops, con NaN donde no hay seg_stats para ese arrival_seq
    stops_with_seg = (
        stops_seq.merge(seg_stats, on="arrival_seq", how="left")
        .sort_values("arrival_seq")
        .reset_index(drop=True)
    )

    # Muestrear shape para búsqueda de índice más cercano (máx ~80 pts)
    step = max(1, len(shape_pts) // 80)
    sampled = shape_pts[::step]
    sampled_idx = list(range(0, len(shape_pts), step))

    # Para cada parada: índice del shape más cercano + distancia (para decidir si el shape es válido ahí)
    NEAR_THRESHOLD_M = 300.0
    stop_bindings: list[tuple[int, float]] = []  # (shape_idx, distance_m)
    for _, stop in stops_with_seg.iterrows():
        s_lat, s_lon = float(stop["lat"]), float(stop["lon"])
        best_j, best_d = 0, float("inf")
        for j, p in enumerate(sampled):
            d = haversine_m(s_lat, s_lon, p[0], p[1])
            if d < best_d:
                best_d = d
                best_j = j
        shape_idx = sampled_idx[best_j] if sampled_idx else 0
        stop_bindings.append((shape_idx, best_d))

    # Forzar monótono no-decreciente en shape_idx (evita slices invertidos)
    for i in range(1, len(stop_bindings)):
        if stop_bindings[i][0] < stop_bindings[i - 1][0]:
            prev_idx = stop_bindings[i - 1][0]
            prev_d = stop_bindings[i][1]
            stop_bindings[i] = (prev_idx, prev_d)

    segments: list[dict] = []
    n_seg = len(stops_with_seg) - 1
    for i in range(n_seg):
        start_idx, start_d = stop_bindings[i]
        end_idx, end_d = stop_bindings[i + 1]
        end_idx_excl = min(end_idx + 1, len(shape_pts))

        start_near = start_d < NEAR_THRESHOLD_M
        end_near = end_d < NEAR_THRESHOLD_M

        # Si ambos endpoints calzan al shape, usar slice del shape
        # Si no, usar stops rectos entre las dos paradas
        s1 = stops_with_seg.iloc[i]
        s2 = stops_with_seg.iloc[i + 1]
        if start_near and end_near and end_idx_excl > start_idx:
            slice_pts = shape_pts[start_idx:end_idx_excl]
        else:
            slice_pts = [
                [float(s1["lat"]), float(s1["lon"])],
                [float(s2["lat"]), float(s2["lon"])],
            ]
        if len(slice_pts) < 2:
            continue

        med_tt = s1["med_tt"]
        med_dist = s1["med_dist"]
        has_data = pd.notna(med_tt) and pd.notna(med_dist) and float(med_tt) > 0
        if has_data:
            speed = float(med_dist) / float(med_tt)
            if mean_speed > 0:
                intensity = max(0.0, min(1.0, (mean_speed - speed) / mean_speed))
            else:
                intensity = 0.0
            speed_kmh: float | None = round(speed * 3.6, 1)
            tt_val: float | None = round(float(med_tt), 1)
        else:
            intensity = 0.0
            speed_kmh = None
            tt_val = None

        segments.append({
            "points": [[p[0], p[1]] for p in slice_pts],
            "intensity": round(intensity, 3),
            "speed_kmh": speed_kmh,
            "med_tt": tt_val,
        })
    return segments


def _build_heatmap_points(stops_seq, seg_stats) -> list[list[float]]:
    """[[lat, lon, intensity], ...] para leaflet.heat.

    Intensidad = cuánto más lento que la velocidad media de la línea (0 = media, 1 = detenido).
    Solo emite segmentos más lentos que la media (intensity > 0.1).
    Mergear por arrival_seq para evitar alineación posicional cuando faltan segmentos.
    """
    if len(stops_seq) < 2 or "arrival_seq" not in stops_seq.columns:
        return []

    total_tt = float(seg_stats["med_tt"].sum())
    total_dist = float(seg_stats["med_dist"].sum())
    if total_tt <= 0 or total_dist <= 0:
        return []
    mean_speed = total_dist / total_tt

    stops_with_seg = (
        stops_seq.merge(seg_stats, on="arrival_seq", how="inner")
        .sort_values("arrival_seq")
        .reset_index(drop=True)
    )
    if len(stops_with_seg) < 2:
        return []

    points: list[list[float]] = []
    for i in range(len(stops_with_seg) - 1):
        s1 = stops_with_seg.iloc[i]
        s2 = stops_with_seg.iloc[i + 1]
        med_tt = float(s1["med_tt"])
        med_dist = float(s1["med_dist"])
        if med_tt <= 0:
            continue
        speed = med_dist / med_tt
        intensity = max(0.0, min(1.0, (mean_speed - speed) / mean_speed))
        if intensity <= 0.1:
            continue
        mid_lat = (float(s1["lat"]) + float(s2["lat"])) / 2
        mid_lon = (float(s1["lon"]) + float(s2["lon"])) / 2
        points.append([round(mid_lat, 6), round(mid_lon, 6), round(intensity, 3)])
    return points


# ---------------------------------------------------------------------------
# Simulación por línea
# ---------------------------------------------------------------------------
def simulate_route(route_short_name: str) -> dict:
    # Fase 1b: cache en runtime (dict) y fallback a disco
    if route_short_name in _RESULT_CACHE:
        return _RESULT_CACHE[route_short_name]
    cached = _load_cached(route_short_name)
    if cached is not None:
        _RESULT_CACHE[route_short_name] = cached
        return cached

    if _SF is None:
        return {"error": _LOAD_ERROR or "Datos no disponibles."}

    r_data = _SF[(_SF["route_short_name"] == route_short_name) & _SF["stop_A_lat"].notna()]
    if r_data.empty:
        return {"error": f"La línea {route_short_name} no tiene datos de segmentos."}

    stops_seq, seg_stats = build_route_data(r_data)
    if stops_seq.empty or seg_stats.empty:
        return {"error": f"La línea {route_short_name} no tiene secuencia de paradas usable."}

    total_obs = float(seg_stats["med_tt"].sum())

    # Fase 2a: grafo SIN OSRM usando precompute global (graph_edges, stop_routes, all_stops)
    G, nearby, stop_routes = build_bus_graph(
        _SF, stops_seq, use_osrm=False, precomputed=_GRAPH_INPUTS
    )
    zones = find_bottleneck_zones(seg_stats, stops_seq)
    proposals = propose_alternatives(zones, stops_seq, seg_stats, G, nearby, stop_routes)

    osrm_url = _pick_osrm_url()
    routing = (
        "osrm-local" if osrm_url == DEFAULT_OSRM_URL
        else "osrm-public" if osrm_url == PUBLIC_OSRM_URL
        else "sin-osrm"
    )

    # Fase 0a (validada): ruta actual priorizando trace GPS observada.
    # Si no hay, cae a GTFS shape; si no calza, a stops rectos.
    current_geometry, geometry_source = _resolve_current_shape(
        route_short_name, r_data, stops_seq, osrm_url
    )
    routing_label = geometry_source

    # Fase 0c (C): segmentar y pegar velocidad para colorear por tramo.
    # Si la source es trace observada, usar los segments pre-calculados con velocidad real.
    # Si no (gtfs/stops), calcular alineando stops_seq con el shape.
    if geometry_source == "observed" and route_short_name in _OBSERVED_TRACES:
        current_segments = _intensity_from_observed_segments(
            _OBSERVED_TRACES[route_short_name].get("segments", [])
        )
    else:
        current_segments = _shape_to_segments(current_geometry, stops_seq, seg_stats)

    # Fase 0b: presupuesto OSRM dedicado por propuesta (no compartido)
    out_proposals = []
    for p in proposals:
        a = (p["lat_A"], p["lon_A"])
        b = (p["lat_B"], p["lon_B"])
        mids = [(ns["lat"], ns["lon"]) for ns in p["new_stops"]]
        mids += [(es["lat"], es["lon"]) for es in p["extra_stops_needed"]]
        ordered = _order_intermediates(a, b, mids)
        seq = [a] + ordered + [b]

        proposed_geometry = _snap(seq, osrm_url, [PROPOSAL_OSRM_BUDGET])

        # Cordura espacial: descartar rodeos
        original_dist = float(p.get("original_dist", 0.0))
        proposed_dist = _path_len_m([(c[0], c[1]) for c in proposed_geometry])
        if original_dist > 0 and proposed_dist > original_dist * DETOUR_MAX_FACTOR:
            continue

        out_proposals.append({
            "zone": p["zone"],
            "lat_A": float(p["lat_A"]), "lon_A": float(p["lon_A"]),
            "lat_B": float(p["lat_B"]), "lon_B": float(p["lon_B"]),
            "original_tt": float(p["original_tt"]),
            "alternative_tt": float(p["alternative_tt"]),
            "savings": float(p["savings"]),
            "max_gap": float(p["max_gap"]),
            "removed_stops": [
                {"stop_id": str(s["stop_id"]), "lat": float(s["lat"]), "lon": float(s["lon"])}
                for s in p["removed_stops"]
            ],
            "new_stops": [
                {"stop_id": str(s["stop_id"]), "lat": float(s["lat"]), "lon": float(s["lon"]),
                 "lines": [str(x) for x in s["lines"]], "is_new": bool(s["is_new"])}
                for s in p["new_stops"]
            ],
            "extra_stops_needed": [
                {"stop_id": str(s["stop_id"]), "lat": float(s["lat"]), "lon": float(s["lon"]),
                 "lines": [str(x) for x in s["lines"]], "reason": str(s["reason"])}
                for s in p["extra_stops_needed"]
            ],
            "proposed_geometry": proposed_geometry,
        })

    total_savings = sum(p["savings"] for p in out_proposals)

    # Fase 0c: heatmap de congestión por segmento
    heatmap_points = _build_heatmap_points(stops_seq, seg_stats)

    result = {
        "route": str(route_short_name),
        "total_obs_s": total_obs,
        "n_stops": int(len(stops_seq)),
        "routing": routing_label,
        "stops_seq": [
            {"stop_id": str(r["stop_id"]), "lat": float(r["lat"]), "lon": float(r["lon"])}
            for _, r in stops_seq.iterrows()
        ],
        "current_geometry": current_geometry,
        "current_geometry_source": geometry_source,
        "current_segments": current_segments,
        "heatmap_points": heatmap_points,
        "proposals": out_proposals,
        "total_savings_s": float(total_savings),
        "total_savings_pct": round(total_savings / total_obs * 100, 1) if total_obs else 0.0,
    }

    # Fase 1b: guardar en cache en memoria
    _RESULT_CACHE[route_short_name] = result
    return result


# ---------------------------------------------------------------------------
# Routing A → B entre dos puntos arbitrarios del mapa
# ---------------------------------------------------------------------------
def _median_observed_speed_mps() -> float:
    """Mediana de (distance_m / travel_time_observed) sobre segments_features.

    Es la velocidad típica de un colectivo urbano según el modelo. Se cachea en
    el módulo para no recalcularla en cada request.
    """
    cached = getattr(_median_observed_speed_mps, "_cached", None)
    if cached is not None:
        return cached
    if _SF is None:
        _median_observed_speed_mps._cached = 5.5  # ~20 km/h fallback
        return _median_observed_speed_mps._cached
    sf = _SF.dropna(subset=["distance_m", "travel_time_observed"])
    sf = sf[(sf["distance_m"] > 0) & (sf["travel_time_observed"] > 0)]
    speeds = sf["distance_m"] / sf["travel_time_observed"]
    _median_observed_speed_mps._cached = float(speeds.median()) if len(speeds) else 5.5
    return _median_observed_speed_mps._cached


def _segment_geometry(geometry: list[list[float]], speed_kmh: float) -> list[dict]:
    """Parte la polyline en chunks de ~8 puntos con velocidad uniforme.

    Para visualizar la ruta A→B con el mismo formato que las líneas existentes
    (segmentos coloreados por velocidad). Aquí todos los segmentos tienen la
    velocidad media observada del modelo, así que el color será homogéneo.
    """
    if len(geometry) < 2:
        return []
    CHUNK = 8
    out: list[dict] = []
    n = len(geometry)
    speed_mps = speed_kmh / 3.6 if speed_kmh > 0 else 0.0
    for start in range(0, n - 1, CHUNK):
        end = min(start + CHUNK + 1, n)
        pts = geometry[start:end]
        dist_m = sum(
            haversine_m(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
            for i in range(len(pts) - 1)
        )
        dt_s = dist_m / speed_mps if speed_mps > 0 else 0.0
        out.append({
            "points": pts,
            "speed_kmh": round(speed_kmh, 1),
            "intensity": 0.0,
            "med_tt": round(dt_s, 1),
        })
    return out


def route_between(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> dict:
    """Traza una ruta de colectivo entre dos puntos arbitrarios del mapa.

    Usa OSRM para la geometría (calles reales) y la velocidad media observada
    del modelo para estimar el tiempo que tardaría un colectivo.

    A diferencia de ``simulate_route`` (que simula una línea existente), esta
    función propone un recorrido nuevo óptimo según la red vial.
    """
    if _SF is None:
        return {"error": _LOAD_ERROR or "Datos no disponibles."}

    osrm_url = _pick_osrm_url()
    if not osrm_url:
        return {"error": "OSRM no disponible. Levantá el docker para usar esta función."}

    # 1. Geometría OSRM entre A y B
    res = get_route(a_lon, a_lat, b_lon, b_lat, osrm_url=osrm_url, timeout=OSRM_TIMEOUT)
    if not res or not res.get("geometry"):
        return {"error": "OSRM no pudo trazar la ruta entre esos puntos."}

    geometry_lonlat = res["geometry"]  # [[lon, lat], ...]
    geometry = [[p[1], p[0]] for p in geometry_lonlat]  # → [[lat, lon], ...]
    distance_m = float(res["distance_m"])
    duration_car_s = float(res["duration_s"])

    # 2. Tiempo estimado de colectivo usando la velocidad del modelo
    bus_speed_mps = _median_observed_speed_mps()
    bus_speed_kmh = bus_speed_mps * 3.6
    bus_duration_s = distance_m / bus_speed_mps if bus_speed_mps > 0 else duration_car_s

    # 3. Segmentos para colorear
    segments = _segment_geometry(geometry, bus_speed_kmh)

    return {
        "from": {"lat": float(a_lat), "lon": float(a_lon)},
        "to": {"lat": float(b_lat), "lon": float(b_lon)},
        "geometry": geometry,
        "segments": segments,
        "distance_m": round(distance_m, 1),
        "duration_car_s": round(duration_car_s, 1),
        "duration_bus_s": round(bus_duration_s, 1),
        "bus_speed_kmh": round(bus_speed_kmh, 1),
        "n_points": len(geometry),
        "routing": "osrm-local" if osrm_url == DEFAULT_OSRM_URL else "osrm",
    }
