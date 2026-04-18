## 1. Project Setup

- [ ] 1.1 Create `analysis/` directory with `__init__.py`, `load.py`, `metrics.py`, `visualize.py`
- [ ] 1.2 Add analysis dependencies to `requirements.txt`: `pandas>=2.0`, `matplotlib>=3.7`, `jupyter>=1.0`
- [ ] 1.3 Install new dependencies and verify imports work

## 2. GTFS Loader (`analysis/load.py`)

- [ ] 2.1 Implement `load_gtfs(data_dir)` that discovers the latest `feed_gtfs_frequency/<timestamp>/` folder and reads all CSV files into a dict of DataFrames with UTF-8 encoding
- [ ] 2.2 Add dtype casting: IDs to string, lat/lon to float, `route_type` to category, `headway_secs` to int
- [ ] 2.3 Implement service type resolution: join `calendar.csv` to trips, mapping HI→"weekday", SI→"saturday", DI→"sunday", FI→"holiday" as a `service_type` column
- [ ] 2.4 Implement `build_scheduled_headway(df_frequencies, df_trips, df_routes, df_agency)` that joins frequencies→trips→routes→agency and produces `df_scheduled_headway` with `route_short_name`, `agency_name`, `start_time`, `end_time`, `scheduled_headway_min`, `service_type`
- [ ] 2.5 Add `period` tagging to `df_scheduled_headway`: "peak" for windows overlapping 7-9am or 5-7pm, "offpeak" otherwise

## 3. Vehicle Tracker (`analysis/load.py`)

- [ ] 3.1 Implement `load_vehicle_positions(data_dir, downsample=10)` that reads all JSON snapshots from `vehicle_positions_simple/`, parses `snapshot_time` from filenames, concatenates into `df_vehicle_pos`
- [ ] 3.2 Add dtype casting: lat/lon to float, speed to float, direction to int, route_id/route_short_name/agency_name to categorical, vehicle ID to string
- [ ] 3.3 Add `vehicle_timestamp` column converted from Unix epoch `timestamp` field to datetime
- [ ] 3.4 Add snapshot count validation: warn and set a `low_confidence` flag if fewer than 10 snapshots loaded

## 4. Route Scorer (`analysis/metrics.py`)

- [ ] 4.1 Implement `compute_actual_headways(df_vehicle_pos)` that sorts by route+direction+vehicle_timestamp and computes time gaps between consecutive buses as `actual_headway_min`
- [ ] 4.2 Implement adherence ratio calculation: match actual headways to scheduled headways by route and time-of-day, compute `adherence_ratio = actual / scheduled`
- [ ] 4.3 Implement bunching detection: flag `is_bunching = True` where `adherence_ratio < 0.5`
- [ ] 4.4 Implement `score_routes(df_actual_headway, df_vehicle_pos)` producing `df_route_score` with: `route_short_name`, `agency_name`, `avg_adherence`, `adherence_std`, `bunching_rate`, `avg_speed_kmh`, `median_headway_min`, `num_observations`
- [ ] 4.5 Implement crowding proxy: `P(adherence_ratio > 1.5) × log(num_active_buses + 1)`
- [ ] 4.6 Implement priority score: `0.4 × max(0, avg_adherence - 1) + 0.4 × crowding_proxy + 0.2 × bunching_rate`, with `priority_rank` column
- [ ] 4.7 Implement time-of-day breakdown: `df_route_period_score` aggregated by route × peak/offpeak

## 5. Visualization Helpers (`analysis/visualize.py`)

- [ ] 5.1 Implement `plot_headway_distribution(df_scheduled_headway)` — histogram of scheduled headways across all routes
- [ ] 5.2 Implement `plot_worst_peak_headways(df_scheduled_headway, top_n=20)` — horizontal bar chart of routes with worst peak headways
- [ ] 5.3 Implement `plot_adherence_distribution(df_actual_headway)` — histogram of adherence ratios with >1.0 region highlighted
- [ ] 5.4 Implement `plot_bunching_by_route(df_route_score, top_n=20)` — bar chart of bunching rates
- [ ] 5.5 Implement `plot_adherence_by_hour(df_actual_headway, routes, top_n=5)` — line plot of adherence by hour-of-day for worst routes
- [ ] 5.6 Implement `plot_priority_ranking(df_route_score, top_n=20)` — styled table + horizontal bar chart of priority scores

## 6. EDA Notebook (`analysis/eda.ipynb`)

- [ ] 6.1 Create notebook with imports from `analysis.load`, `analysis.metrics`, `analysis.visualize`
- [ ] 6.2 Add data loading section: load GTFS, load vehicle positions, print data freshness summary (GTFS timestamp, snapshot range, route counts)
- [ ] 6.3 Add schedule analysis section: call schedule visualizations, display key stats
- [ ] 6.4 Add reliability section: compute headways/adherence/bunching, call reliability visualizations, handle insufficient-data case with informative message
- [ ] 6.5 Add route ranking section: compute `df_route_score`, display top-20 ranked routes table and priority chart
- [ ] 6.6 Smoke-test the notebook end-to-end with the existing sample data
