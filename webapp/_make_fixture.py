"""Genera outputs/*.parquet SINTÉTICOS para smoke-test del front.
Reemplazar por los parquets reales (segments_features / route_scores) cuando estén.

Uso: python webapp/_make_fixture.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)

rng = np.random.default_rng(7)


def corridor(route, trip, lat0, lon0, dlat, dlon, n, tts=None):
    """Cadena de n paradas a lo largo de un corredor. tts = lista de travel times por segmento."""
    rows = []
    lats = [lat0 + i * dlat for i in range(n)]
    lons = [lon0 + i * dlon for i in range(n)]
    for seq in range(n - 1):
        tt = tts[seq] if tts is not None else 60.0
        # 3+ observaciones por arista (build_bus_graph exige n>=3)
        for _ in range(4):
            rows.append({
                "route_short_name": route,
                "trip_id": trip,
                "arrival_seq": seq,
                "next_seq": seq + 1,
                "stop_id_A": f"{route}_S{seq}",
                "stop_id_B": f"{route}_S{seq+1}",
                "stop_A_lat": lats[seq], "stop_A_lon": lons[seq],
                "stop_B_lat": lats[seq+1], "stop_B_lon": lons[seq+1],
                "travel_time_observed": tt + rng.normal(0, 3),
                "distance_m": 300.0 + rng.normal(0, 10),
            })
    return rows, lats, lons


rows = []
# Línea 999 — corredor con plateau de congestión en el medio (segmentos 4-6)
tt_999 = [12, 18, 30, 60, 300, 320, 300, 60, 30, 18, 12]
r999, lats, lons = corridor("999", "T999", -34.609, -58.380, 0.0, -0.0035, 12, tts=tt_999)
rows += r999

# Paradas paralelas (otra línea "777") un poco al norte, que bordean el cuello → atajo
for seq in range(4, 8):
    a_lat, a_lon = lats[seq] + 0.0012, lons[seq]
    b_lat, b_lon = lats[seq+1] + 0.0012, lons[seq+1]
    for _ in range(4):
        rows.append({
            "route_short_name": "777", "trip_id": "T777",
            "arrival_seq": seq, "next_seq": seq + 1,
            "stop_id_A": f"ALT_S{seq}", "stop_id_B": f"ALT_S{seq+1}",
            "stop_A_lat": a_lat, "stop_A_lon": a_lon,
            "stop_B_lat": b_lat, "stop_B_lon": b_lon,
            "travel_time_observed": 55.0 + rng.normal(0, 3),
            "distance_m": 300.0 + rng.normal(0, 8),
        })

# Línea 152 — otro corredor sin cuellos marcados
r152, _, _ = corridor("152", "T152", -34.600, -58.420, -0.003, 0.001, 10)
rows += r152

sf = pd.DataFrame(rows)
sf.to_parquet(OUT / "segments_features.parquet", index=False)

scores = pd.DataFrame([
    {"route_id": "r999", "route_short_name": "999", "direction_id": 0,
     "spatial_deviation_m": 180.0, "temporal_deviation_pct": 40.0, "efficiency_score": 520.0},
    {"route_id": "r152", "route_short_name": "152", "direction_id": 0,
     "spatial_deviation_m": 90.0, "temporal_deviation_pct": 15.0, "efficiency_score": 210.0},
    {"route_id": "r777", "route_short_name": "777", "direction_id": 0,
     "spatial_deviation_m": 60.0, "temporal_deviation_pct": 8.0, "efficiency_score": 95.0},
]).sort_values("efficiency_score", ascending=False)
scores.to_parquet(OUT / "route_scores.parquet", index=False)

print("Fixture escrito:")
print(" segments_features.parquet:", len(sf), "filas")
print(" route_scores.parquet:", len(scores), "filas")
