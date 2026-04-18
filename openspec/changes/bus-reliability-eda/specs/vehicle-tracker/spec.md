## ADDED Requirements

### Requirement: Load vehicle position snapshots
The system SHALL load all JSON snapshot files from `data/colectivos/vehicle_positions_simple/`, concatenate them into a single DataFrame, and parse the snapshot timestamp from each filename.

#### Scenario: Loading multiple snapshots
- **WHEN** the load function is called with the vehicle positions directory path
- **THEN** it reads every `YYYYMMDD_HHMMSS.json` file, parses the filename into a `snapshot_time` datetime column, concatenates all records, and returns a single `df_vehicle_pos` DataFrame

#### Scenario: Downsampled loading
- **WHEN** a `downsample` parameter is provided (default: 10)
- **THEN** only every Nth snapshot file is loaded (e.g., downsample=10 loads every 10th file), reducing data volume while preserving temporal coverage

### Requirement: Snapshot timestamp column
Each row in `df_vehicle_pos` SHALL carry a `snapshot_time` column parsed from the source filename, and a `vehicle_timestamp` column from the per-vehicle `timestamp` field (Unix epoch).

#### Scenario: Timestamp columns present
- **WHEN** vehicle position data is loaded
- **THEN** the DataFrame contains `snapshot_time` (datetime, from filename) and `vehicle_timestamp` (datetime, from record `timestamp` field converted from Unix epoch)

### Requirement: Typed vehicle position DataFrame
The system SHALL cast vehicle position columns to appropriate dtypes.

#### Scenario: Numeric and categorical casting
- **WHEN** vehicle positions are loaded
- **THEN** `latitude` and `longitude` are float; `speed` is float (m/s); `direction` is int; `route_id`, `route_short_name`, `agency_name`, `trip_headsign` are categorical or string; `id` (vehicle ID) is string

### Requirement: Actual headway computation
The system SHALL compute actual headways between consecutive buses on the same route, direction, and snapshot period.

#### Scenario: Headway between consecutive buses
- **WHEN** `df_vehicle_pos` is available and contains at least 2 snapshots for a given route+direction
- **THEN** for each route+direction, buses are sorted by `vehicle_timestamp`, and the time gap between consecutive buses is computed as `actual_headway_min`

#### Scenario: Insufficient data warning
- **WHEN** fewer than 10 snapshots are available
- **THEN** the system issues a warning that headway measurements may be unreliable and returns the metrics with a `low_confidence` flag

### Requirement: Adherence ratio calculation
The system SHALL compute an adherence ratio per observation: `actual_headway_min / scheduled_headway_min`.

#### Scenario: Adherence ratio derivation
- **WHEN** actual headways and scheduled headways are both available for a route+time window
- **THEN** each actual headway measurement is matched to the corresponding scheduled headway (by route and time-of-day) and divided to produce an `adherence_ratio` where 1.0 = perfect adherence, >1.0 = late/gapped, <0.5 = bunching

### Requirement: Bunching detection
The system SHALL flag observations where `adherence_ratio < 0.5` as bunching incidents.

#### Scenario: Bunching flag
- **WHEN** adherence ratios are computed
- **THEN** any observation with `adherence_ratio < 0.5` is flagged with `is_bunching = True`, indicating two buses are running closer than half the scheduled headway
