"""Precomputa simulate_route() para todas las líneas y vuelca JSON en outputs/cache/.

Uso:
    python webapp/precompute_cache.py            # todas las líneas
    python webapp/precompute_cache.py 185C 152   # solo las pasadas
    python webapp/precompute_cache.py --force    # sobreescribe los JSON existentes

Conveniente correr esto una vez (con OSRM local si querés snaps prolijos en propuestas)
y después el server sirve los JSON estáticamente: runtime O(1) por request.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "webapp"))

import os  # noqa: E402
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webapp.settings")
import django  # noqa: E402
django.setup()

from simulator import engine  # noqa: E402


def precompute(lines: list[str], force: bool = False) -> None:
    cache_dir = engine.CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not engine.data_ready():
        print(f"ERROR: {engine.load_error()}")
        sys.exit(1)

    if not lines:
        lines = [l["route_short_name"] for l in engine.list_available_lines()]

    print(f"Precomputando {len(lines)} líneas → {cache_dir}/")
    print(f"  shape_lookup: {len(engine._SHAPE_LOOKUP) if engine._SHAPE_LOOKUP else 0} shapes")
    print(f"  trip_shape: {len(engine._TRIP_SHAPE) if engine._TRIP_SHAPE else 0} trips")
    print()

    # Vaciar cache en memoria para que cada simulate_route corra realmente
    engine._RESULT_CACHE.clear()

    n_ok = n_skip = n_err = 0
    t_total = time.monotonic()
    for i, route in enumerate(lines, 1):
        out_path = engine._cache_path(route)
        if out_path.exists() and not force:
            n_skip += 1
            print(f"[{i:>3}/{len(lines)}] {route:>10}  SKIP (ya cacheado)")
            continue

        t0 = time.monotonic()
        # Si --force, borrar JSON cacheado para que simulate_route no lo lea
        if force and out_path.exists():
            out_path.unlink()
        # Bypass del cache en memoria también
        engine._RESULT_CACHE.pop(route, None)
        result = engine.simulate_route(route)
        dt = time.monotonic() - t0

        if "error" in result:
            n_err += 1
            print(f"[{i:>3}/{len(lines)}] {route:>10}  ERROR ({dt:.1f}s): {result['error']}")
            continue

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f)
        # forzar hot path en runtime
        engine._RESULT_CACHE[route] = result

        n_ok += 1
        n_props = len(result.get("proposals", []))
        src = result.get("current_geometry_source", "?")
        n_pts = len(result.get("current_geometry", []))
        n_heat = len(result.get("heatmap_points", []))
        print(
            f"[{i:>3}/{len(lines)}] {route:>10}  OK  {dt:5.1f}s "
            f"· {n_pts:>4} pts ({src}) · {n_props} props · {n_heat} heat"
        )

    dt_total = time.monotonic() - t_total
    print()
    print(f"Listo en {dt_total:.1f}s · {n_ok} nuevas · {n_skip} skip · {n_err} errores")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    force = "--force" in sys.argv
    precompute(args, force=force)


if __name__ == "__main__":
    main()
