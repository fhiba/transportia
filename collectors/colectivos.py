"""
Collectors for /colectivos endpoints.

Endpoints:
  GET /colectivos/vehiclePositions       → GTFS-RT protobuf OR JSON (json=true)
  GET /colectivos/vehiclePositionsSimple → JSON always
  GET /colectivos/serviceAlerts          → GTFS-RT protobuf OR JSON (json=true)
  GET /colectivos/feed-gtfs              → GTFS static ZIP  (6h interval)
  GET /colectivos/feed-gtfs-frequency    → GTFS static ZIP  (6h interval)

Storage format decision:
  - vehiclePositions / serviceAlerts: request JSON directly (json=true).
    If API returns protobuf regardless, decode via gtfs_realtime_pb2.
  - feed-gtfs / feed-gtfs-frequency: save ZIP + extract CSVs.
    Best for EDA: the extracted stop_times.txt, trips.txt, etc. are flat CSVs.
"""
import logging
import threading

from collectors.base import BaseCollector
from utils.storage import save_json, save_gtfs_zip, save_raw
from utils.proto_decoder import decode_gtfs_rt
from config import INTERVALS

logger = logging.getLogger(__name__)


class ColectivosCollector(BaseCollector):

    # ------------------------------------------------------------------ #
    #  vehiclePositions  (every 30s)                                       #
    # ------------------------------------------------------------------ #
    def collect_vehicle_positions(self):
        r = self.get("/colectivos/vehiclePositions", params={"json": 1})
        if r is None:
            return
        ct = r.headers.get("Content-Type", "")
        if "json" in ct:
            saved = save_json("colectivos/vehicle_positions", r.json())
        else:
            # API returned protobuf despite json=1 — decode it
            decoded = decode_gtfs_rt(r.content)
            if decoded:
                saved = save_json("colectivos/vehicle_positions", decoded)
            else:
                saved = save_raw("colectivos/vehicle_positions", r.content)
        logger.debug("vehiclePositions → %s", saved.name)

    # ------------------------------------------------------------------ #
    #  vehiclePositionsSimple  (every 30s)                                 #
    # ------------------------------------------------------------------ #
    def collect_vehicle_positions_simple(self):
        r = self.get("/colectivos/vehiclePositionsSimple")
        if r is None:
            return
        saved = save_json("colectivos/vehicle_positions_simple", r.json())
        logger.debug("vehiclePositionsSimple → %s", saved.name)

    # ------------------------------------------------------------------ #
    #  serviceAlerts  (every 2min)                                         #
    # ------------------------------------------------------------------ #
    def collect_service_alerts(self):
        r = self.get("/colectivos/serviceAlerts", params={"json": 1})
        if r is None:
            return
        ct = r.headers.get("Content-Type", "")
        if "json" in ct:
            saved = save_json("colectivos/service_alerts", r.json())
        else:
            decoded = decode_gtfs_rt(r.content)
            if decoded:
                saved = save_json("colectivos/service_alerts", decoded)
            else:
                saved = save_raw("colectivos/service_alerts", r.content)
        logger.debug("serviceAlerts → %s", saved.name)

    # ------------------------------------------------------------------ #
    #  feed-gtfs  (every 6h)                                               #
    # ------------------------------------------------------------------ #
    def collect_feed_gtfs(self):
        r = self.get("/colectivos/feed-gtfs")
        if r is None:
            return
        saved = save_gtfs_zip("colectivos/feed_gtfs", r.content)
        logger.info("feed-gtfs → %s (%.1f KB)", saved.name, len(r.content) / 1024)

    # ------------------------------------------------------------------ #
    #  feed-gtfs-frequency  (every 6h)                                     #
    # ------------------------------------------------------------------ #
    def collect_feed_gtfs_frequency(self):
        r = self.get("/colectivos/feed-gtfs-frequency")
        if r is None:
            return
        saved = save_gtfs_zip("colectivos/feed_gtfs_frequency", r.content)
        logger.info("feed-gtfs-frequency → %s (%.1f KB)", saved.name, len(r.content) / 1024)

    # ------------------------------------------------------------------ #
    #  Spawn all threads                                                   #
    # ------------------------------------------------------------------ #
    def start(self) -> list[threading.Thread]:
        jobs = [
            (self.collect_vehicle_positions,        INTERVALS["vehicle_positions"],       "vehicle_positions"),
            (self.collect_vehicle_positions_simple, INTERVALS["vehicle_positions_simple"],"vehicle_positions_simple"),
            (self.collect_service_alerts,           INTERVALS["service_alerts"],          "service_alerts"),
            (self.collect_feed_gtfs,                INTERVALS["gtfs_static"],             "feed_gtfs"),
            (self.collect_feed_gtfs_frequency,      INTERVALS["gtfs_static"],             "feed_gtfs_frequency"),
        ]
        threads = []
        for fn, interval, name in jobs:
            t = threading.Thread(target=self.run_loop, args=(fn, interval, name), daemon=True, name=name)
            t.start()
            threads.append(t)
        return threads
