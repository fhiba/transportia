## Why

Buenos Aires colectivos suffer from unreliable schedules and overcrowding, but there's no data-driven pipeline to measure which routes are worst, diagnose why, or prioritize optimization. We have a collector pulling GTFS schedules, real-time vehicle positions, and aggregate ridership data — but no analysis layer to turn raw files into actionable route-level metrics.

## What Changes

- Add an EDA pipeline (Python module + Jupyter notebook) that loads collected GTFS and real-time data into structured DataFrames
- Derive per-route reliability metrics: headway adherence (actual vs scheduled), bunching rate, and average speed
- Derive a crowding proxy from headway gaps (longer-than-scheduled gaps = more passenger accumulation)
- Produce a route-level scoring table that ranks bus lines by optimization priority
- Visualize patterns: time-of-day reliability curves, bunching hotspots, route speed profiles

## Capabilities

### New Capabilities

- `gtfs-loader`: Loads GTFS static files (routes, trips, frequencies, stops, shapes) from collected data into clean DataFrames with proper dtypes and encoding
- `vehicle-tracker`: Loads and concatenates real-time vehicle position snapshots into a single time-series DataFrame with derived headway measurements
- `route-scorer`: Computes per-route reliability metrics (adherence, bunching, speed) and produces a ranked optimization priority table
- `eda-notebook`: Jupyter notebook tying the pipeline together with visualizations for schedule analysis, reliability diagnostics, and route ranking

### Modified Capabilities

(none — this is greenfield)

## Impact

- New Python module under an `analysis/` directory
- Requires `pandas`, `matplotlib`/`plotly` (or similar) — new dependencies beyond current `requirements.txt`
- Reads from existing `data/` directory structure produced by the collector
- No changes to the collector itself
- The full GTFS `stop_times.csv` (1.3 GB) is excluded in favor of the frequency-based feed (~16 MB) for practical memory use
- Real-time analysis requires 1-2 days of collected vehicle position data (the 30s-interval snapshots)
