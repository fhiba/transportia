## ADDED Requirements

### Requirement: End-to-end EDA notebook
The system SHALL provide a Jupyter notebook (`eda.ipynb`) that imports from `analysis.load` and `analysis.metrics` and runs the full pipeline from data loading to route ranking.

#### Scenario: Notebook execution
- **WHEN** the notebook is opened and all cells are executed
- **THEN** it loads GTFS data, loads vehicle positions, computes headways, derives metrics, produces visualizations, and displays the top-20 ranked routes — all without external manual steps beyond running the cells

### Requirement: Schedule analysis visualization
The notebook SHALL include visualizations of the scheduled service: headway distributions by route, peak vs off-peak frequency comparison, and agency-level service summary.

#### Scenario: Schedule plots rendered
- **WHEN** the schedule analysis section is executed
- **THEN** plots are rendered showing: (1) histogram of scheduled headways across all routes, (2) top-20 routes by worst peak headway, (3) agency-level bubble chart of routes vs average headway

### Requirement: Reliability diagnostic visualization
The notebook SHALL include visualizations of real-time reliability: adherence distribution, bunching rate by route, and time-of-day reliability curves.

#### Scenario: Reliability plots rendered
- **WHEN** the reliability section is executed with sufficient real-time data
- **THEN** plots are rendered showing: (1) histogram of adherence ratios (highlighting >1.0 as late), (2) top-20 routes by bunching rate, (3) adherence by hour-of-day for the top-5 worst routes

#### Scenario: Insufficient real-time data
- **WHEN** the reliability section is executed with fewer than 10 snapshots
- **THEN** a message is displayed indicating more data collection is needed, and schedule-only analysis is shown instead

### Requirement: Route ranking visualization
The notebook SHALL include a visualization of the final route ranking with the priority score breakdown.

#### Scenario: Ranking table and chart
- **WHEN** the route scoring section is executed
- **THEN** a styled table of top-20 routes is displayed with columns for each metric component, alongside a horizontal bar chart of priority scores

### Requirement: Data freshness indicator
The notebook SHALL display metadata about the loaded data: collection time range, number of snapshots, number of routes with real-time data.

#### Scenario: Data summary printed
- **WHEN** data loading completes
- **THEN** a summary is printed showing: GTFS feed timestamp, real-time data time range (first to last snapshot), total snapshots loaded, unique routes in schedule vs real-time data
