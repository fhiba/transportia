import sys
import logging
from pathlib import Path

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import joblib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import OUTPUTS_DIR, haversine_m
from osrm_routing import is_osrm_available, get_route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

MAX_GAP_M = 500
MEDIAN_SPEED_MPS = 4.6
TT_CAP = 600


def load_worst_route(sf: pd.DataFrame, route_scores_path: Path = None):
    if route_scores_path and route_scores_path.exists():
        scores = pd.read_parquet(route_scores_path)
        if "route_short_name" in scores.columns and not scores.empty:
            valid_routes = set(sf["route_short_name"].dropna().unique())
            scores = scores[scores["route_short_name"].isin(valid_routes)]
        if "route_short_name" in scores.columns and not scores.empty:
            worst_route = scores.iloc[0]["route_short_name"]
            log.info("Worst route from route_scores: %s (score=%.0f)", worst_route, scores.iloc[0].get("efficiency_score", 0))
            r_data = sf[(sf["route_short_name"] == worst_route) & sf["stop_A_lat"].notna()]
            if not r_data.empty:
                return r_data, worst_route
    log.warning("Could not load worst route from scores — falling back to most observed route")
    route_counts = sf[sf["stop_A_lat"].notna()].groupby("route_short_name").size().sort_values(ascending=False)
    if route_counts.empty:
        return None, None
    worst_route = route_counts.index[0]
    return sf[(sf["route_short_name"] == worst_route) & sf["stop_A_lat"].notna()], worst_route


def build_route_data(r_data: pd.DataFrame):
    best_trip = r_data.groupby("trip_id").size().sort_values(ascending=False).index[0]
    trip_data = r_data[r_data["trip_id"] == best_trip]
    stops_seq = trip_data.groupby("arrival_seq").agg(
        stop_id=("stop_id_A", "first"),
        lat=("stop_A_lat", "median"),
        lon=("stop_A_lon", "median"),
    ).sort_index().reset_index(drop=True)
    seg_stats = trip_data[trip_data["next_seq"] == trip_data["arrival_seq"] + 1].groupby(
        "arrival_seq"
    ).agg(
        med_tt=("travel_time_observed", "median"),
        med_dist=("distance_m", "median"),
    ).sort_index().reset_index(drop=True)
    return stops_seq, seg_stats


def build_bus_graph(sf, stops_seq, use_osrm=False):
    all_a = sf[["stop_id_A", "stop_A_lat", "stop_A_lon"]].dropna().drop_duplicates("stop_id_A")
    all_a.columns = ["stop_id", "lat", "lon"]
    all_b = sf[["stop_id_B", "stop_B_lat", "stop_B_lon"]].dropna().drop_duplicates("stop_id_B")
    all_b.columns = ["stop_id", "lat", "lon"]
    all_stops = pd.concat([all_a, all_b]).drop_duplicates("stop_id")

    bbox_lat = (stops_seq["lat"].min() - 0.015, stops_seq["lat"].max() + 0.015)
    bbox_lon = (stops_seq["lon"].min() - 0.015, stops_seq["lon"].max() + 0.015)
    nearby = all_stops[all_stops["lat"].between(*bbox_lat) & all_stops["lon"].between(*bbox_lon)]

    stop_routes = sf.dropna(subset=["stop_A_lat"]).groupby("stop_id_A")[
        "route_short_name"
    ].apply(lambda x: list(x.unique())[:5]).to_dict()

    graph_edges = sf.dropna(subset=["stop_A_lat"]).groupby(
        ["stop_id_A", "stop_id_B"]
    ).agg(
        med_tt=("travel_time_observed", "median"),
        n=("travel_time_observed", "count"),
    ).reset_index()
    graph_edges = graph_edges[graph_edges["n"] >= 3]

    zone_ids = set(nearby["stop_id"])
    zone_edges = graph_edges[
        graph_edges["stop_id_A"].isin(zone_ids) & graph_edges["stop_id_B"].isin(zone_ids)
    ]

    G = nx.DiGraph()
    for _, r in zone_edges.iterrows():
        G.add_edge(r["stop_id_A"], r["stop_id_B"], weight=r["med_tt"], observed=True)

    route_stop_ids = set(stops_seq["stop_id"])
    from scipy.spatial import cKDTree
    coords = np.radians(nearby[["lat", "lon"]].values)
    tree = cKDTree(coords)
    radius_rad = (MAX_GAP_M * 2) / 6_371_000

    pairs = tree.query_pairs(radius_rad)
    n_osrm_calls = 0
    max_osrm_calls = 500

    for i, j in pairs:
        s = nearby.iloc[i]
        t = nearby.iloc[j]
        if s["stop_id"] in route_stop_ids and t["stop_id"] in route_stop_ids:
            continue
        if not G.has_edge(s["stop_id"], t["stop_id"]):
            d_haversine = haversine_m(s["lat"], s["lon"], t["lat"], t["lon"])

            est_tt = d_haversine / MEDIAN_SPEED_MPS
            est_dist = d_haversine
            geometry = None

            if use_osrm and n_osrm_calls < max_osrm_calls:
                route = get_route(s["lon"], s["lat"], t["lon"], t["lat"])
                if route:
                    est_tt = route["duration_s"]
                    est_dist = route["distance_m"]
                    geometry = route["geometry"]
                    n_osrm_calls += 1

            G.add_edge(s["stop_id"], t["stop_id"], weight=est_tt, observed=False, distance=est_dist, geometry=geometry)
            G.add_edge(t["stop_id"], s["stop_id"], weight=est_tt, observed=False, distance=est_dist, geometry=geometry)

    log.info("OSRM calls used: %d", n_osrm_calls)
    return G, nearby, stop_routes


def find_bottleneck_zones(seg_stats, stops_seq, p90=None):
    if p90 is None:
        p90 = seg_stats["med_tt"].quantile(0.85)
    mean_speed = seg_stats["med_dist"].sum() / seg_stats["med_tt"].sum()

    zones = []
    i = 0
    while i < len(seg_stats):
        if seg_stats.iloc[i]["med_tt"] > p90:
            start = i
            while i < len(seg_stats) and seg_stats.iloc[i]["med_tt"] > p90 * 0.6:
                i += 1
            end = min(i, len(seg_stats))
            if end - start >= 1:
                zone_tt = seg_stats.iloc[start:end]["med_tt"].sum()
                zone_dist = seg_stats.iloc[start:end]["med_dist"].sum()
                expected_tt = zone_dist / mean_speed
                if zone_tt > expected_tt * 1.3 and zone_tt > 120:
                    zones.append({
                        "start_idx": start,
                        "end_idx": end,
                        "tt": zone_tt,
                        "dist": zone_dist,
                        "stop_A_idx": start,
                        "stop_B_idx": end,
                    })
        else:
            i += 1
    return zones


def propose_alternatives(zones, stops_seq, seg_stats, G, nearby, stop_routes):
    route_stop_ids = set(stops_seq["stop_id"])
    all_stops_map = nearby.set_index("stop_id")[["lat", "lon"]].to_dict("index")
    proposals = []

    for z in zones:
        a_idx = z["stop_A_idx"]
        b_idx = z["stop_B_idx"]
        a = stops_seq.iloc[a_idx]
        b = stops_seq.iloc[b_idx]
        a_id = a["stop_id"]
        b_id = b["stop_id"]
        original_tt = z["tt"]

        removed_stops = []
        for idx in range(a_idx + 1, b_idx):
            removed_stops.append({
                "stop_id": stops_seq.iloc[idx]["stop_id"],
                "lat": stops_seq.iloc[idx]["lat"],
                "lon": stops_seq.iloc[idx]["lon"],
            })

        removed_dist = 0
        for idx in range(a_idx, b_idx):
            s1 = stops_seq.iloc[idx]
            s2 = stops_seq.iloc[idx + 1]
            removed_dist += haversine_m(s1["lat"], s1["lon"], s2["lat"], s2["lon"])

        try:
            path = nx.shortest_path(G, a_id, b_id, weight="weight")
            path_tt = sum(G[path[i]][path[i + 1]]["weight"] for i in range(len(path) - 1))
        except (nx.NetworkXNoPath, KeyError):
            path = [a_id, b_id]
            path_tt = original_tt * 0.5

        path_geometry = []
        for k in range(len(path) - 1):
            edge_data = G[path[k]][path[k + 1]]
            geom = edge_data.get("geometry")
            if geom:
                path_geometry.extend(geom)
            else:
                s_info = all_stops_map.get(path[k], {"lat": None, "lon": None})
                t_info = all_stops_map.get(path[k + 1], {"lat": None, "lon": None})
                if s_info.get("lat") and t_info.get("lat"):
                    path_geometry.append([s_info["lon"], s_info["lat"]])
                    path_geometry.append([t_info["lon"], t_info["lat"]])

        new_stops = []
        for sid in path[1:-1]:
            if sid in all_stops_map:
                info = all_stops_map[sid]
                routes = stop_routes.get(sid, [])
            else:
                match = stops_seq[stops_seq["stop_id"] == sid]
                if not match.empty:
                    info = {"lat": match.iloc[0]["lat"], "lon": match.iloc[0]["lon"]}
                else:
                    continue
                routes = []
            new_stops.append({
                "stop_id": sid,
                "lat": info["lat"],
                "lon": info["lon"],
                "lines": routes[:5],
                "is_new": sid not in route_stop_ids,
            })

        all_points = [(a["lat"], a["lon"])]
        for ns in new_stops:
            all_points.append((ns["lat"], ns["lon"]))
        all_points.append((b["lat"], b["lon"]))

        max_gap = 0
        for i in range(len(all_points) - 1):
            gap = haversine_m(all_points[i][0], all_points[i][1],
                              all_points[i + 1][0], all_points[i + 1][1])
            max_gap = max(max_gap, gap)

        extra_stops_needed = []
        for i in range(len(all_points) - 1):
            gap = haversine_m(all_points[i][0], all_points[i][1],
                              all_points[i + 1][0], all_points[i + 1][1])
            if gap > MAX_GAP_M:
                mid_lat = (all_points[i][0] + all_points[i + 1][0]) / 2
                mid_lon = (all_points[i][1] + all_points[i + 1][1]) / 2
                close = nearby[
                    (abs(nearby["lat"] - mid_lat) < 0.003)
                    & (abs(nearby["lon"] - mid_lon) < 0.003)
                    & (~nearby["stop_id"].isin(route_stop_ids))
                ]
                if not close.empty:
                    best = close.iloc[0]
                    extra_stops_needed.append({
                        "stop_id": best["stop_id"],
                        "lat": best["lat"],
                        "lon": best["lon"],
                        "lines": stop_routes.get(best["stop_id"], [])[:5],
                        "reason": f"Gap {gap:.0f}m > {MAX_GAP_M}m",
                    })

        savings = max(0, original_tt - path_tt)

        proposals.append({
            "zone": f"seq {a_idx}->{b_idx}",
            "stop_A": a_id,
            "stop_B": b_id,
            "lat_A": a["lat"],
            "lon_A": a["lon"],
            "lat_B": b["lat"],
            "lon_B": b["lon"],
            "original_tt": original_tt,
            "original_dist": removed_dist,
            "removed_stops": removed_stops,
            "new_stops": new_stops,
            "extra_stops_needed": extra_stops_needed,
            "alternative_tt": path_tt,
            "savings": savings,
            "max_gap": max_gap,
            "path": path,
            "path_geometry": path_geometry,
        })

    return proposals


def main():
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    sf_path = OUTPUTS_DIR / "segments_features.parquet"
    if not sf_path.exists():
        log.error("Missing %s", sf_path)
        sys.exit(1)

    log.info("Loading segments features...")
    sf = pd.read_parquet(sf_path, columns=[
        "route_short_name", "trip_id", "arrival_seq", "next_seq",
        "stop_id_A", "stop_id_B", "stop_A_lat", "stop_A_lon",
        "stop_B_lat", "stop_B_lon", "travel_time_observed", "distance_m",
    ])

    use_osrm = is_osrm_available()
    if use_osrm:
        log.info("OSRM server available — using real street routing")
    else:
        log.warning("OSRM server NOT available — falling back to haversine estimates")
        log.warning("Start with: docker compose -f docker-compose.osrm.yml up -d")

    log.info("Loading worst route from route_scores...")
    scores_path = OUTPUTS_DIR / "route_scores.parquet"
    r_data, route_name = load_worst_route(sf, scores_path)
    if r_data is None:
        log.error("No route data found")
        sys.exit(1)

    stops_seq, seg_stats = build_route_data(r_data)
    total_obs = seg_stats["med_tt"].sum()
    log.info("Route %s: %d stops, %.0fs (%.1f min) total", route_name, len(stops_seq), total_obs, total_obs / 60)

    log.info("Building bus-stop graph...")
    G, nearby, stop_routes = build_bus_graph(sf, stops_seq, use_osrm=use_osrm)
    log.info("Graph: %d nodes, %d edges", G.number_of_nodes(), G.number_of_edges())

    log.info("Finding bottleneck zones...")
    zones = find_bottleneck_zones(seg_stats, stops_seq)
    log.info("Found %d bottleneck zones", len(zones))

    log.info("Proposing alternatives...")
    proposals = propose_alternatives(zones, stops_seq, seg_stats, G, nearby, stop_routes)

    total_savings = sum(p["savings"] for p in proposals)

    print("\n" + "=" * 70)
    print(f"CHECKPOINT — Route Simulator (Linea {route_name})")
    print("=" * 70)
    print(f"\nRoute {route_name}: {len(stops_seq)} stops, {total_obs:.0f}s ({total_obs/60:.1f} min)")
    print(f"Routing: {'OSRM (real streets)' if use_osrm else 'Haversine (straight line)'}")
    print(f"Bottleneck zones: {len(proposals)}")
    print()

    for p in proposals:
        print(f"  Zona {p['zone']}: {p['stop_A']} -> {p['stop_B']}")
        print(f"    Problema: {p['original_tt']:.0f}s en {p['original_dist']:.0f}m")
        if p["removed_stops"]:
            rids = [s["stop_id"] for s in p["removed_stops"]]
            print(f"    Eliminar: {', '.join(rids)}")
        new_labels = []
        for ns in p["new_stops"]:
            tag = "(NUEVA)" if ns["is_new"] else ""
            lines_str = ", ".join(ns["lines"][:3]) if ns["lines"] else "?"
            new_labels.append(f"{ns['stop_id']}{tag} [{lines_str}]")
        if new_labels:
            print(f"    Agregar: {', '.join(new_labels)}")
        for es in p["extra_stops_needed"]:
            lines_str = ", ".join(es["lines"][:3]) if es["lines"] else "?"
            print(f"    + Intermedia: {es['stop_id']} [{lines_str}] ({es['reason']})")
        print(f"    Tiempo original: {p['original_tt']:.0f}s | Alternativa: {p['alternative_tt']:.0f}s")
        print(f"    Ahorro: {p['savings']:.0f}s ({p['savings']/60:.1f} min) | Max gap: {p['max_gap']:.0f}m")
        print()

    print(f"  AHORRO TOTAL: {total_savings:.0f}s ({total_savings/60:.1f} min) — "
          f"{total_savings/total_obs*100:.1f}%")

    fig, ax = plt.subplots(figsize=(14, 10))
    ax.grid(True, alpha=0.3, color="gray", linestyle="--")

    ax.plot(stops_seq["lon"], stops_seq["lat"], "b-o", markersize=3,
            linewidth=1.5, alpha=0.5, zorder=2, label=f"Ruta actual {route_name}")

    for p in proposals:
        ax.plot(
            [p["lon_A"], p["lon_B"]],
            [p["lat_A"], p["lat_B"]],
            color="red", linewidth=3, alpha=0.3, zorder=3,
        )
        for rs in p["removed_stops"]:
            ax.plot(rs["lon"], rs["lat"], "x", color="red", markersize=10,
                    markeredgewidth=2, zorder=5)

        alt_lons = [p["lon_A"]]
        alt_lats = [p["lat_A"]]
        if p.get("path_geometry"):
            alt_lons = [pt[0] for pt in p["path_geometry"]]
            alt_lats = [pt[1] for pt in p["path_geometry"]]
        else:
            for ns in p["new_stops"]:
                alt_lons.append(ns["lon"])
                alt_lats.append(ns["lat"])
            for es in p["extra_stops_needed"]:
                alt_lons.append(es["lon"])
                alt_lats.append(es["lat"])
            alt_lons.append(p["lon_B"])
            alt_lats.append(p["lat_B"])
        ax.plot(alt_lons, alt_lats, "g-", linewidth=2.5, alpha=0.7, zorder=4)

        for ns in p["new_stops"]:
            if ns["is_new"]:
                ax.plot(ns["lon"], ns["lat"], "*", color="limegreen", markersize=14,
                        markeredgecolor="black", markeredgewidth=0.8, zorder=6)
                lines_str = ",".join(ns["lines"][:2])
                ax.annotate(lines_str, (ns["lon"], ns["lat"]),
                            fontsize=7, fontweight="bold", color="green",
                            xytext=(8, 8), textcoords="offset points", zorder=7,
                            bbox=dict(boxstyle="round,pad=0.15", facecolor="white", alpha=0.7))

        for es in p["extra_stops_needed"]:
            ax.plot(es["lon"], es["lat"], "*", color="gold", markersize=12,
                    markeredgecolor="black", markeredgewidth=0.8, zorder=6)

        mid_lon = (p["lon_A"] + p["lon_B"]) / 2
        mid_lat = (p["lat_A"] + p["lat_B"]) / 2
        ax.annotate(
            f"-{p['savings']:.0f}s",
            (mid_lon, mid_lat),
            fontsize=9, fontweight="bold", color="darkgreen",
            ha="center", va="bottom", zorder=8,
            bbox=dict(boxstyle="round,pad=0.2", facecolor="white", alpha=0.8),
        )

    from matplotlib.lines import Line2D
    legend_items = [
        Line2D([0], [0], color="blue", marker="o", markersize=3, linewidth=1.5,
               alpha=0.5, label=f"Ruta actual {route_name}"),
        Line2D([0], [0], color="red", linewidth=3, alpha=0.3, label="Zona congestionada"),
        Line2D([0], [0], color="green", linewidth=2.5, alpha=0.7, label="Ruta propuesta"),
        Line2D([0], [0], marker="x", color="red", markersize=10, linestyle="None",
               markeredgewidth=2, label="Parada eliminada"),
        Line2D([0], [0], marker="*", color="limegreen", markersize=14, linestyle="None",
               markeredgecolor="black", label="Parada nueva (otra linea)"),
        Line2D([0], [0], marker="*", color="gold", markersize=12, linestyle="None",
               markeredgecolor="black", label="Parada intermedia nueva"),
    ]
    ax.legend(handles=legend_items, loc="upper left", fontsize=8)

    margin = 0.004
    ax.set_xlim(stops_seq["lon"].min() - margin, stops_seq["lon"].max() + margin)
    ax.set_ylim(stops_seq["lat"].min() - margin, stops_seq["lat"].max() + margin)
    ax.set_aspect("equal")

    try:
        import contextily as cx
        cx.add_basemap(ax, crs="EPSG:4326", source=cx.providers.CartoDB.Positron)
    except Exception:
        pass

    ax.set_xlabel("Longitud")
    ax.set_ylabel("Latitud")
    ax.set_title(f"Linea {route_name}: Propuestas de optimizacion")
    plt.tight_layout()

    fig_path = OUTPUTS_DIR / "route_comparison.png"
    plt.savefig(fig_path, dpi=150)
    plt.close()
    log.info("Saved %s", fig_path)

    if proposals:
        n_zoom = min(len(proposals), 4)
        fig_zoom, axes_zoom = plt.subplots(1, n_zoom, figsize=(6 * n_zoom, 6))
        if n_zoom == 1:
            axes_zoom = [axes_zoom]

        for ax_z, p in zip(axes_zoom, proposals[:n_zoom]):
            ax_z.grid(True, alpha=0.3, color="gray", linestyle="--")
            all_lats = [p["lat_A"], p["lat_B"]] + [ns["lat"] for ns in p["new_stops"]] + [es["lat"] for es in p["extra_stops_needed"]]
            all_lons = [p["lon_A"], p["lon_B"]] + [ns["lon"] for ns in p["new_stops"]] + [es["lon"] for es in p["extra_stops_needed"]]
            margin_z = 0.003

            nearby_route = stops_seq[
                (stops_seq["lat"].between(min(all_lats) - margin_z, max(all_lats) + margin_z))
                & (stops_seq["lon"].between(min(all_lons) - margin_z, max(all_lons) + margin_z))
            ]
            ax_z.plot(nearby_route["lon"], nearby_route["lat"], "b-o", markersize=4,
                      linewidth=2, alpha=0.6, zorder=2)

            for rs in p["removed_stops"]:
                ax_z.plot(rs["lon"], rs["lat"], "x", color="red", markersize=12,
                          markeredgewidth=2, zorder=5)

            alt_lons = [p["lon_A"]]
            alt_lats = [p["lat_A"]]
            if p.get("path_geometry"):
                alt_lons = [pt[0] for pt in p["path_geometry"]]
                alt_lats = [pt[1] for pt in p["path_geometry"]]
            else:
                for ns in p["new_stops"]:
                    alt_lons.append(ns["lon"])
                    alt_lats.append(ns["lat"])
                for es in p["extra_stops_needed"]:
                    alt_lons.append(es["lon"])
                    alt_lats.append(es["lat"])
                alt_lons.append(p["lon_B"])
                alt_lats.append(p["lat_B"])
            ax_z.plot(alt_lons, alt_lats, "g-", linewidth=3, alpha=0.8, zorder=4)

            for idx_p, ns in enumerate(p["new_stops"]):
                if ns["is_new"]:
                    ax_z.plot(ns["lon"], ns["lat"], "*", color="limegreen", markersize=16,
                              markeredgecolor="black", markeredgewidth=0.8, zorder=6)
                    lines_str = ",".join(ns["lines"][:2])
                    offset_y = 8 + (idx_p % 3) * 12
                    ax_z.annotate(lines_str, (ns["lon"], ns["lat"]),
                                  fontsize=8, fontweight="bold", color="green",
                                  xytext=(8, offset_y), textcoords="offset points", zorder=7,
                                  bbox=dict(boxstyle="round,pad=0.15", facecolor="white", alpha=0.7))

            for es in p["extra_stops_needed"]:
                ax_z.plot(es["lon"], es["lat"], "*", color="gold", markersize=14,
                          markeredgecolor="black", markeredgewidth=0.8, zorder=6)

            ax_z.set_xlim(min(all_lons) - margin_z, max(all_lons) + margin_z)
            ax_z.set_ylim(min(all_lats) - margin_z, max(all_lats) + margin_z)
            ax_z.set_aspect("equal")
            ax_z.set_title(f"Zona {p['zone']}: -{p['savings']:.0f}s", fontsize=10)

            try:
                import contextily as cx
                cx.add_basemap(ax_z, crs="EPSG:4326", source=cx.providers.CartoDB.Positron)
            except Exception:
                pass

        plt.suptitle(f"Linea {route_name}: Zoom en zonas de optimizacion", fontsize=12, fontweight="bold")
        plt.tight_layout()
        fig_zoom_path = OUTPUTS_DIR / "route_comparison_zoom.png"
        plt.savefig(fig_zoom_path, dpi=150)
        plt.close()
        log.info("Saved %s", fig_zoom_path)

    print("=" * 70)
    print("\n>>> Review the proposals above. If OK, proceed to notebook.")


if __name__ == "__main__":
    main()
