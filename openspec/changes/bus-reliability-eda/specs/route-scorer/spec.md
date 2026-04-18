## ADDED Requirements

### Requirement: Route-level reliability metrics
The system SHALL aggregate real-time observations into per-route metrics in a `df_route_score` DataFrame.

#### Scenario: Metric computation
- **WHEN** `df_actual_headway` is available with adherence ratios and bunching flags
- **THEN** a `df_route_score` DataFrame is produced with one row per route containing: `route_short_name`, `agency_name`, `avg_adherence`, `adherence_std`, `bunching_rate`, `avg_speed_kmh`, `median_headway_min`, `num_observations`

### Requirement: Crowding proxy score
The system SHALL compute a crowding proxy per route based on the frequency and severity of headway gaps.

#### Scenario: Crowding proxy derivation
- **WHEN** adherence ratios are available
- **THEN** a `crowding_proxy` score is computed per route as the fraction of observations where `adherence_ratio > 1.5` (i.e., buses arriving 50%+ later than scheduled), scaled by `demand_weight = log(num_active_buses + 1)`

#### Scenario: Crowding proxy without real-time data
- **WHEN** no real-time data is available
- **THEN** `crowding_proxy` is set to NaN and the route is excluded from crowding-based ranking

### Requirement: Optimization priority ranking
The system SHALL rank routes by an optimization priority score combining reliability, crowding, and impact.

#### Scenario: Priority score formula
- **WHEN** all per-route metrics are computed
- **THEN** a `priority_score` is calculated as: `0.4 × max(0, avg_adherence - 1.0) + 0.4 × crowding_proxy + 0.2 × bunching_rate`, and routes are sorted by descending score

#### Scenario: Priority ranking output
- **WHEN** the scoring is complete
- **THEN** `df_route_score` includes a `priority_rank` column (1 = worst, N = best) and the top-K routes are easily accessible

### Requirement: Time-of-day reliability breakdown
The system SHALL compute reliability metrics per route broken down by time period (peak/offpeak).

#### Scenario: Period-level aggregation
- **WHEN** adherence ratios are tagged with peak/offpeak periods
- **THEN** a `df_route_period_score` DataFrame is produced with `route_short_name`, `period` (peak/offpeak), `avg_adherence`, `bunching_rate`, `avg_speed_kmh` — enabling identification of time-specific problems
