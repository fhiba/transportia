"""
Collectors for /transito/v1 endpoints.

Endpoints:
  GET /transito/v1/cortes          → JSON  (road closures; changes daily)
  GET /transito/v1/semaforos       → JSON  (traffic signals near a point)
  GET /transito/v1/estacionamientos → JSON/GeoJSON (parking near a point)
  GET /transito/v1/eventos         → JSON  (events; needs month + provider)

Storage format decision:
  All return JSON → saved as JSON files.
  GeoJSON from estacionamientos is also valid JSON and works with geopandas.
"""
import logging
import threading
from datetime import datetime, timezone

from collectors.base import BaseCollector
from utils.storage import save_json
from config import INTERVALS, CENTER_LAT, CENTER_LON, RADIUS, EVENTO_PROVIDERS

logger = logging.getLogger(__name__)


class TransitoCollector(BaseCollector):

    # ------------------------------------------------------------------ #
    #  Cortes (road closures)  — every 5min                               #
    # ------------------------------------------------------------------ #
    def collect_cortes(self):
        r = self.get("/transito/v1/cortes")
        if r is None:
            return
        saved = save_json("transito/v1/cortes", r.json())
        logger.debug("cortes → %s", saved.name)

    # ------------------------------------------------------------------ #
    #  Semaforos (traffic signals)  — every 5min                          #
    # ------------------------------------------------------------------ #
    def collect_semaforos(self):
        params = {"lat": CENTER_LAT, "lon": CENTER_LON, "radius": RADIUS}
        r = self.get("/transito/v1/semaforos", params=params)
        if r is None:
            return
        saved = save_json("transito/v1/semaforos", r.json())
        logger.debug("semaforos → %s", saved.name)

    # ------------------------------------------------------------------ #
    #  Estacionamientos (parking)  — every 10min                          #
    # ------------------------------------------------------------------ #
    def collect_estacionamientos(self):
        params = {
            "x": CENTER_LON,
            "y": CENTER_LAT,
            "srid": 4326,
            "radio": min(RADIUS, 100),  # API enforces max 100
            "formato": "json",
        }
        r = self.get("/transito/v1/estacionamientos", params=params)
        if r is None:
            return
        saved = save_json("transito/v1/estacionamientos", r.json())
        logger.debug("estacionamientos → %s", saved.name)

    # ------------------------------------------------------------------ #
    #  Eventos  — every 15min, all providers, current month               #
    # ------------------------------------------------------------------ #
    def collect_eventos(self):
        month = datetime.now(timezone.utc).strftime("%Y%m")
        for provider in EVENTO_PROVIDERS:
            params = {"month": month, "provider": provider}
            r = self.get("/transito/v1/eventos", params=params)
            if r is None:
                continue
            saved = save_json(f"transito/v1/eventos/provider_{provider}", r.json())
            logger.debug("eventos/provider_%s → %s", provider, saved.name)

    # ------------------------------------------------------------------ #
    #  Spawn threads                                                       #
    # ------------------------------------------------------------------ #
    def start(self) -> list[threading.Thread]:
        jobs = [
            (self.collect_cortes,            INTERVALS["cortes"],           "cortes"),
            (self.collect_semaforos,         INTERVALS["semaforos"],        "semaforos"),
            (self.collect_estacionamientos,  INTERVALS["estacionamientos"], "estacionamientos"),
            (self.collect_eventos,           INTERVALS["eventos"],          "eventos"),
        ]
        threads = []
        for fn, interval, name in jobs:
            t = threading.Thread(target=self.run_loop, args=(fn, interval, name), daemon=True, name=name)
            t.start()
            threads.append(t)
        return threads
