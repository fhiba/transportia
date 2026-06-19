import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

DEFAULT_OSRM_URL = "http://localhost:5000"
PUBLIC_OSRM_URL = "https://router.project-osrm.org"


def get_route(
    lon1: float,
    lat1: float,
    lon2: float,
    lat2: float,
    osrm_url: str = DEFAULT_OSRM_URL,
    timeout: float = 5.0,
) -> Optional[dict]:
    url = f"{osrm_url}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
    params = {"overview": "full", "geometries": "geojson"}
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        route = data["routes"][0]
        geometry = route["geometry"]["coordinates"] if route.get("geometry") else []
        return {
            "distance_m": route["distance"],
            "duration_s": route["duration"],
            "geometry": geometry,
        }
    except (requests.RequestException, KeyError, IndexError) as e:
        log.debug("OSRM route failed %.4f,%.4f -> %.4f,%.4f: %s", lon1, lat1, lon2, lat2, e)
        return None


def batch_routes(
    pairs: list[tuple[float, float, float, float]],
    osrm_url: str = DEFAULT_OSRM_URL,
    delay: float = 0.05,
) -> list[Optional[dict]]:
    results = []
    for i, (lon1, lat1, lon2, lat2) in enumerate(pairs):
        result = get_route(lon1, lat1, lon2, lat2, osrm_url)
        results.append(result)
        if delay > 0 and (i + 1) % 10 == 0:
            time.sleep(delay)
    return results


def get_route_distance_m(
    lon1: float, lat1: float, lon2: float, lat2: float, osrm_url: str = DEFAULT_OSRM_URL,
) -> Optional[float]:
    route = get_route(lon1, lat1, lon2, lat2, osrm_url)
    if route:
        return route["distance_m"]
    return None


def get_route_duration_s(
    lon1: float, lat1: float, lon2: float, lat2: float, osrm_url: str = DEFAULT_OSRM_URL,
) -> Optional[float]:
    route = get_route(lon1, lat1, lon2, lat2, osrm_url)
    if route:
        return route["duration_s"]
    return None


def get_route_waypoints(
    coords: list,
    osrm_url: str = DEFAULT_OSRM_URL,
    timeout: float = 10.0,
) -> Optional[dict]:
    """Get a single route through multiple waypoints. coords = list of (lon, lat) tuples."""
    if len(coords) < 2:
        return None
    coords_str = ";".join(f"{lon},{lat}" for lon, lat in coords)
    url = f"{osrm_url}/route/v1/driving/{coords_str}"
    params = {"overview": "full", "geometries": "geojson"}
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        route = data["routes"][0]
        geometry = route["geometry"]["coordinates"] if route.get("geometry") else []
        return {
            "distance_m": route["distance"],
            "duration_s": route["duration"],
            "geometry": geometry,
        }
    except (requests.RequestException, KeyError, IndexError) as e:
        log.debug("OSRM waypoints route failed: %s", e)
        return None


def match_route(
    coords: list,
    osrm_url: str = DEFAULT_OSRM_URL,
    timeout: float = 10.0,
    radius: float = 50.0,
) -> Optional[dict]:
    """Map-match a trace of (lon, lat) points to the road network.

    Usa el servicio /match de OSRM en vez de /route. /route obliga a *pasar* por cada
    waypoint, así que cuando dos paradas consecutivas caen sobre lados opuestos de una
    avenida ancha (9 de Julio, Av. de Mayo) OSRM mete una vuelta a la manzana → se ven
    "dientes de peine"/rulos. /match ajusta el rastro a las calles sin forzar el paso
    exacto, devolviendo una polyline limpia y continua.
    """
    if len(coords) < 2:
        return None
    coords_str = ";".join(f"{lon},{lat}" for lon, lat in coords)
    radiuses = ";".join(str(radius) for _ in coords)
    url = f"{osrm_url}/match/v1/driving/{coords_str}"
    params = {
        "overview": "full",
        "geometries": "geojson",
        "tidy": "true",       # descarta puntos redundantes/outliers del rastro
        "gaps": "ignore",     # tratar como un único rastro continuo
        "radiuses": radiuses,
    }
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("matchings"):
            return None
        geometry: list = []
        for m in data["matchings"]:
            geom = m.get("geometry", {}).get("coordinates") or []
            if geometry and geom and geometry[-1] == geom[0]:
                geom = geom[1:]
            geometry.extend(geom)
        return {"geometry": geometry}
    except (requests.RequestException, KeyError, IndexError) as e:
        log.debug("OSRM match failed: %s", e)
        return None


def is_osrm_available(osrm_url: str = DEFAULT_OSRM_URL) -> bool:
    try:
        resp = requests.get(f"{osrm_url}/route/v1/driving/-58.38,-34.60;-58.37,-34.61", timeout=3)
        return resp.status_code == 200
    except requests.RequestException:
        return False
