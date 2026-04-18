## Context

The transportia project already collects data from Buenos Aires' transport API into a `data/` directory. The data includes:

- **GTFS static schedules** (frequency-based feed): routes, trips, stops, frequencies, shapes — CSV files in `data/colectivos/feed_gtfs_frequency/<timestamp>/`
- **Real-time vehicle positions** (simple): JSON snapshots every 30s in `data/colectivos/vehicle_positions_simple/`
- **Service alerts**: GTFS-RT JSON in `data/colectivos/service_alerts/`
- **Traffic infrastructure**: traffic light data in `data/transito/v1/semaforos/`
- **Aggregate patterns**: typical-week ridership in `data/datos/movilidad/`

All data is stored locally. No database. The current `requirements.txt` has only `requests`, `gtfs-realtime-bindings`, `protobuf`, and `python-dotenv`.

Key constraints:
- Full GTFS `stop_times.csv` is 1.3 GB — impractical for in-memory analysis. We use the frequency feed (~16 MB) instead.
- Vehicle position `occupancy_status` is always 0 — agencies don't report it. Crowding must be inferred.
- Real-time snapshots are plain JSON arrays with flat records — no nesting.
- GTFS CSVs have encoding issues (UTF-8 bytes displayed as Latin-1: `Ã±` instead of `ñ`).
- Typical-week data uses string-typed numeric fields (`"cantidad": "2126"`).

## Goals / Non-Goals

**Goals:**
- Load all relevant data sources into typed pandas DataFrames with a single function call per source
- Derive scheduled headway profiles per route (peak vs off-peak from the frequency feed)
- Derive actual headways from real-time snapshots by tracking bus positions over time
- Compute per-route reliability metrics: adherence ratio, bunching rate, average speed
- Produce a crowding proxy from headway deviations
- Rank routes by an optimization priority score combining reliability, crowding, and ridership
- Provide a Jupyter notebook with visualizations that make the analysis explorable

**Non-Goals:**
- Real-time dashboards or streaming analysis (batch EDA only)
- Machine learning prediction models
- Changes to the data collector
- Loading the full 1.3 GB GTFS `stop_times.csv`
- Geospatial rendering (maps) — focus on statistical plots; lat/lon columns are included for future map use
- Optimization algorithms or scheduling solvers

## Decisions

### 1. Pure pandas (no Polars/Dask)

**Choice**: pandas with careful dtype optimization.

**Why**: The largest working dataset after downsampling is ~4.6M vehicle position rows (5-min intervals × 2 days). With categorical dtypes for route IDs and agency names, this fits comfortably in ~500 MB. Polars would be faster but adds a dependency and API complexity that isn't justified for EDA scale.

**Alternatives considered**:
- Polars: faster but overkill for <5M rows and unfamiliar API
- Dask: only needed if we exceed RAM, which we won't
- DuckDB: good for SQL-style analysis, but EDA is inherently iterative/pandas-native

### 2. Downsample real-time data to 5-minute intervals

**Choice**: Keep every 10th snapshot (30s → 5min) during loading.

**Why**: Buses on the same route are 3-30 minutes apart. 5-minute resolution is sufficient to measure headway deviations and detect bunching. This reduces 23M records (2 days × 30s) to ~4.6M — much more manageable. The downsampling factor is configurable.

**Alternatives considered**:
- Load all 23M records: wasteful, slow, no analytical benefit for headway measurement
- 1-minute intervals: marginal improvement over 5 minutes, 5× more data

### 3. Frequency feed over full GTFS

**Choice**: Use `feed_gtfs_frequency/` exclusively for schedule data.

**Why**: The frequency feed has `frequencies.csv` (headway per time window per trip) which is exactly what we need — "how often does this route run during this time period?" The full feed has exact stop times for every trip (1.3 GB) but doesn't add analytical value for reliability scoring. The frequency feed also has simpler calendar structure (4 service types vs 430K exceptions).

### 4. Crowding inference via headway gap proxy

**Choice**: `crowding_proxy = P(actual_headway > scheduled_headway) × demand_weight`.

**Why**: No direct occupancy data is available. The GTFS-RT `occupancy_status` field is universally 0. Research literature shows that headway gaps are a strong proxy for crowding: longer-than-scheduled gaps cause passenger accumulation at stops, leading to overcrowded buses when they finally arrive. The demand weight uses number of active buses per route as a proxy for route importance/popularity.

### 5. Module structure under `analysis/`

**Choice**: Create `analysis/` directory with:
```
analysis/
  __init__.py
  load.py          # gtfs-loader + vehicle-tracker (DataFrame construction)
  metrics.py       # route-scorer (derived metrics + scoring)
  visualize.py     # plot helpers used by the notebook
  eda.ipynb        # the notebook
```

**Why**: Keeps analysis code separate from collector code. `load.py` handles all I/O and DataFrame construction. `metrics.py` is pure computation on DataFrames. `visualize.py` holds reusable plot functions. The notebook ties them together.

**Alternatives considered**:
- Everything in one notebook: unmaintainable, untestable
- Separate package with setup.py: overkill for an analysis module

### 6. Snapshot timestamp parsing from filenames

**Choice**: Parse snapshot time from filename pattern `YYYYMMDD_HHMMSS.json`.

**Why**: The simple vehicle positions JSON doesn't contain a collection timestamp in a metadata wrapper — only per-vehicle `timestamp` fields (which are the GPS timestamps). The filename is the authoritative snapshot time. We parse it into a `datetime` column during loading.

## Risks / Trade-offs

**[Data volume at 30s intervals]** → The downsampling factor (default: 10) is configurable. If finer analysis is needed for a specific route, the loader can accept a route filter to load all snapshots for just that route.

**[Encoding issues in GTFS CSVs]** → Force `encoding='utf-8'` on read. The garbled text (`Ã±`) is from Latin-1/UTF-8 mismatches in the source data — we read as UTF-8 and accept the artifacts, or attempt normalization where critical.

**[Limited real-time data if collector runs briefly]** → The pipeline degrades gracefully: scheduled analysis works with any amount of GTFS data, real-time metrics are only computed if enough snapshots exist. A minimum snapshot count check warns the user.

**[No ground truth for crowding validation]** → The crowding proxy is unvalidated. Document this clearly in the notebook. The metric is a relative ranking tool (route A vs route B), not an absolute passenger count.

**[GTFS feed may have multiple timestamped versions]** → The loader uses the latest extracted folder by default. The GTFS data rarely changes (updated every 6 hours, but the schedule itself changes infrequently).
