"""Capa de servicio del front: lista líneas disponibles y corre el simulador por línea.

Reutiliza la lógica pura de ``scripts/07_simulate_route.py`` (grafo de paradas, detección
de cuellos de botella y propuestas). El grafo se arma SIEMPRE con haversine (cero llamadas
a OSRM en el loop — eso es lo que antes colgaba el simulador). OSRM se usa únicamente al
final, con un tope duro de llamadas, para "snapear" a las calles las dos polylines que se
dibujan (ruta actual + cada propuesta).
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# scripts/ debe estar en sys.path: 07 hace `from utils import ...` y `from osrm_routing import ...`
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from utils import OUTPUTS_DIR, haversine_m  # noqa: E402
from osrm_routing import (  # noqa: E402
    DEFAULT_OSRM_URL,
    PUBLIC_OSRM_URL,
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
MAX_OSRM_CALLS = 8           # tope duro por request
OSRM_TIMEOUT = 6.0           # seg por llamada
WAYPOINT_CHUNK = 80          # OSRM público limita waypoints por request
DEDUPE_EPS_M = 15            # paradas casi-iguales consecutivas
DETOUR_MAX_FACTOR = 1.6      # si la alternativa rodea > 1.6× la original, se descarta


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


def data_ready() -> bool:
    return _SF is not None


def load_error() -> str | None:
    return _LOAD_ERROR


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
    if is_osrm_available(DEFAULT_OSRM_URL):
        return DEFAULT_OSRM_URL
    if is_osrm_available(PUBLIC_OSRM_URL):
        return PUBLIC_OSRM_URL
    return None


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
# Simulación por línea
# ---------------------------------------------------------------------------
def simulate_route(route_short_name: str) -> dict:
    if _SF is None:
        return {"error": _LOAD_ERROR or "Datos no disponibles."}

    r_data = _SF[(_SF["route_short_name"] == route_short_name) & _SF["stop_A_lat"].notna()]
    if r_data.empty:
        return {"error": f"La línea {route_short_name} no tiene datos de segmentos."}

    stops_seq, seg_stats = build_route_data(r_data)
    if stops_seq.empty or seg_stats.empty:
        return {"error": f"La línea {route_short_name} no tiene secuencia de paradas usable."}

    total_obs = float(seg_stats["med_tt"].sum())

    # Grafo SIN OSRM (rápido). OSRM solo al final para dibujar.
    G, nearby, stop_routes = build_bus_graph(_SF, stops_seq, use_osrm=False)
    zones = find_bottleneck_zones(seg_stats, stops_seq)
    proposals = propose_alternatives(zones, stops_seq, seg_stats, G, nearby, stop_routes)

    osrm_url = _pick_osrm_url()
    routing = (
        "osrm-local" if osrm_url == DEFAULT_OSRM_URL
        else "osrm-public" if osrm_url == PUBLIC_OSRM_URL
        else "sin-osrm"
    )
    budget = [MAX_OSRM_CALLS]

    # Ruta actual snapeada a calles (en orden de arrival_seq)
    current_latlon = [(r["lat"], r["lon"]) for _, r in stops_seq.iterrows()]
    current_geometry = _snap(current_latlon, osrm_url, budget)

    out_proposals = []
    for p in proposals:
        a = (p["lat_A"], p["lon_A"])
        b = (p["lat_B"], p["lon_B"])
        mids = [(ns["lat"], ns["lon"]) for ns in p["new_stops"]]
        mids += [(es["lat"], es["lon"]) for es in p["extra_stops_needed"]]
        ordered = _order_intermediates(a, b, mids)
        seq = [a] + ordered + [b]

        proposed_geometry = _snap(seq, osrm_url, budget)

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

    return {
        "route": str(route_short_name),
        "total_obs_s": total_obs,
        "n_stops": int(len(stops_seq)),
        "routing": routing,
        "stops_seq": [
            {"stop_id": str(r["stop_id"]), "lat": float(r["lat"]), "lon": float(r["lon"])}
            for _, r in stops_seq.iterrows()
        ],
        "current_geometry": current_geometry,
        "proposals": out_proposals,
        "total_savings_s": float(total_savings),
        "total_savings_pct": round(total_savings / total_obs * 100, 1) if total_obs else 0.0,
    }
