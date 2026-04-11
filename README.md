# transportia

Data collector for the [Buenos Aires Transport API](https://api-transporte.buenosaires.gob.ar/console). Polls all `/colectivos`, `/datos`, and `/transito` endpoints on configurable intervals and stores the results locally for exploratory data analysis.

## What it collects

| Section | Endpoints | Interval |
|---|---|---|
| `/colectivos` | vehicle positions, service alerts, GTFS static feeds | 30s – 6h |
| `/datos/movilidad` | hourly traffic counts, public transit usage, typical-week patterns, monthly CSVs | 1h – daily |
| `/transito/v1` | road closures, traffic signals, parking, events (all 3 providers) | 5min – 15min |

## Data format

- **JSON** — real-time feeds (vehicle positions, alerts, traffic events). GTFS-RT protobuf is decoded to JSON automatically.
- **CSV** — monthly aggregate reports (direct `pandas.read_csv`).
- **ZIP + extracted CSVs** — GTFS static schedule feeds.

All files land under `data/` mirroring the API path structure:

```
data/
  colectivos/
    vehicle_positions/        20260411_143022.json ...
    vehicle_positions_simple/
    service_alerts/
    feed_gtfs/                20260411_000000.zip + extracted/
    feed_gtfs_frequency/
  datos/movilidad/
    transito/hourly/          20260411_140000.json ...
    transito/mensual/         202603.csv  202604.csv ...
    transporte_publico/daily/
    ...
  transito/v1/
    cortes/
    semaforos/
    estacionamientos/
    eventos/provider_1/
    eventos/provider_201/
    eventos/provider_769/
```

## Setup

**1. Get API credentials**

Register at https://api-transporte.buenosaires.gob.ar/registro — you'll receive a `client_id` and `client_secret` by email.

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Configure**

```bash
cp .env.example .env
# edit .env and set CLIENT_ID and CLIENT_SECRET
```

**4. Collect**

```bash
python collect.py              # runs for 2 hours (default)
python collect.py --duration 4 # run for 4 hours
python collect.py --duration 0 # run until Ctrl+C
```

Logs are written to `collector.log` alongside the data directory.
