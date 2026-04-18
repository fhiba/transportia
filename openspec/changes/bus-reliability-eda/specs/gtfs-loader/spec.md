## ADDED Requirements

### Requirement: Load GTFS reference tables
The system SHALL provide a function that loads all GTFS frequency-feed CSVs from the latest extracted folder into separate typed DataFrames: `df_routes`, `df_agency`, `df_stops`, `df_trips`, `df_frequencies`, `df_shapes`.

#### Scenario: Loading from latest GTFS feed
- **WHEN** the load function is called with the data directory path
- **THEN** it locates the latest timestamped subfolder under `data/colectivos/feed_gtfs_frequency/`, reads all CSV files with UTF-8 encoding, and returns a dictionary of DataFrames with correct dtypes (numeric IDs as int/string, lat/lon as float, times as string)

#### Scenario: Handling encoding artifacts
- **WHEN** CSV files contain garbled UTF-8 text (e.g., `Ã±` instead of `ñ`)
- **THEN** the loader reads with `encoding='utf-8'` and returns the data as-is, logging a warning if mojibake patterns are detected

### Requirement: Typed DataFrames
The system SHALL cast GTFS columns to appropriate dtypes on load.

#### Scenario: Numeric and categorical casting
- **WHEN** DataFrames are loaded
- **THEN** `route_id`, `trip_id`, `stop_id`, `agency_id` are cast to string; `stop_lat`, `stop_lon`, `shape_pt_lat`, `shape_pt_lon` are cast to float; `route_type` is cast to categorical; `headway_secs` is cast to int

### Requirement: Calendar and service type resolution
The system SHALL join the `calendar.csv` service types (HI=weekday, SI=Saturday, DI=Sunday, FI=holiday) to trips so that each trip carries a human-readable service label.

#### Scenario: Service type labeling
- **WHEN** `df_trips` is loaded
- **THEN** each row includes a `service_type` column with values like "weekday", "saturday", "sunday", "holiday" derived from the `calendar.csv` mapping

### Requirement: Scheduled headway aggregation
The system SHALL produce a route-level headway summary by joining frequencies → trips → routes, producing one row per route per time window with `scheduled_headway_min`, `start_time`, `end_time`, and `service_type`.

#### Scenario: Headway summary creation
- **WHEN** `df_frequencies`, `df_trips`, and `df_routes` are available
- **THEN** a `df_scheduled_headway` DataFrame is produced with columns `route_short_name`, `agency_name`, `start_time`, `end_time`, `scheduled_headway_min`, `service_type`, grouping across all trips sharing the same route

#### Scenario: Peak vs off-peak identification
- **WHEN** the headway summary includes time windows
- **THEN** each row is tagged with `period` = "peak" (7-9am, 17-19pm) or "offpeak" (all other times)
