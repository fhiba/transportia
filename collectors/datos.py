"""
Collectors for /datos/movilidad endpoints.

Endpoints:
  GET /datos/movilidad/transito                   → JSON  (hourly count report)
  GET /datos/movilidad/transito/semanaTipica       → JSON  (typical-week pattern)
  GET /datos/movilidad/transito/mensual            → CSV   (current month)
  GET /datos/movilidad/transito/mensual/{anioMes}  → CSV   (historical month)

  GET /datos/movilidad/transportePublico                   → JSON
  GET /datos/movilidad/transportePublico/semanaTipica       → JSON
  GET /datos/movilidad/transportePublico/mensual            → CSV
  GET /datos/movilidad/transportePublico/mensual/{anioMes}  → CSV

Storage format decision:
  - JSON endpoints → saved as JSON files (pandas-friendly via read_json / json_normalize)
  - CSV endpoints  → saved as CSV files (direct pandas read_csv)
  - Historical monthly CSVs fetched once at startup (last 12 months)
"""
import logging
import threading
from datetime import datetime, timezone, timedelta

from collectors.base import BaseCollector
from utils.storage import save_json, save_csv
from config import INTERVALS

logger = logging.getLogger(__name__)


def _last_n_months(n: int = 12) -> list[str]:
    """Return list of YYYYMM strings for last n months."""
    months = []
    now = datetime.now(timezone.utc)
    for i in range(n):
        d = now - timedelta(days=30 * i)
        months.append(d.strftime("%Y%m"))
    return months


class DatosCollector(BaseCollector):

    # ------------------------------------------------------------------ #
    #  Transito                                                            #
    # ------------------------------------------------------------------ #
    def collect_transito_hourly(self):
        r = self.get("/datos/movilidad/transito")
        if r is None:
            return
        saved = save_json("datos/movilidad/transito/hourly", r.json())
        logger.debug("transito/hourly → %s", saved.name)

    def collect_transito_semana_tipica(self):
        r = self.get("/datos/movilidad/transito/semanaTipica")
        if r is None:
            return
        saved = save_json("datos/movilidad/transito/semana_tipica", r.json())
        logger.info("transito/semanaTipica → %s", saved.name)

    def collect_transito_mensual_current(self):
        r = self.get("/datos/movilidad/transito/mensual")
        if r is None:
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        saved = save_csv("datos/movilidad/transito/mensual", r.content, filename=f"current_{ts}.csv")
        logger.info("transito/mensual (current) → %s", saved.name)

    def collect_transito_mensual_historical(self):
        for anio_mes in _last_n_months(12):
            r = self.get(f"/datos/movilidad/transito/mensual/{anio_mes}")
            if r is None:
                continue
            saved = save_csv("datos/movilidad/transito/mensual", r.content, filename=f"{anio_mes}.csv")
            logger.info("transito/mensual/%s → %s", anio_mes, saved.name)

    # ------------------------------------------------------------------ #
    #  Transporte Público                                                  #
    # ------------------------------------------------------------------ #
    def collect_tp_daily(self):
        r = self.get("/datos/movilidad/transportePublico")
        if r is None:
            return
        saved = save_json("datos/movilidad/transporte_publico/daily", r.json())
        logger.debug("transportePublico/daily → %s", saved.name)

    def collect_tp_semana_tipica(self):
        r = self.get("/datos/movilidad/transportePublico/semanaTipica")
        if r is None:
            return
        saved = save_json("datos/movilidad/transporte_publico/semana_tipica", r.json())
        logger.info("transportePublico/semanaTipica → %s", saved.name)

    def collect_tp_mensual_current(self):
        r = self.get("/datos/movilidad/transportePublico/mensual")
        if r is None:
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        saved = save_csv("datos/movilidad/transporte_publico/mensual", r.content, filename=f"current_{ts}.csv")
        logger.info("transportePublico/mensual (current) → %s", saved.name)

    def collect_tp_mensual_historical(self):
        for anio_mes in _last_n_months(12):
            r = self.get(f"/datos/movilidad/transportePublico/mensual/{anio_mes}")
            if r is None:
                continue
            saved = save_csv("datos/movilidad/transporte_publico/mensual", r.content, filename=f"{anio_mes}.csv")
            logger.info("transportePublico/mensual/%s → %s", anio_mes, saved.name)

    # ------------------------------------------------------------------ #
    #  Spawn threads                                                       #
    # ------------------------------------------------------------------ #
    def start(self) -> list[threading.Thread]:
        # Fetch historical CSVs once in background at startup
        for fn, name in [
            (self.collect_transito_mensual_historical, "transito_mensual_historical"),
            (self.collect_tp_mensual_historical,        "tp_mensual_historical"),
        ]:
            t = threading.Thread(target=fn, daemon=True, name=name)
            t.start()

        jobs = [
            (self.collect_transito_hourly,         INTERVALS["movilidad_transito"], "transito_hourly"),
            (self.collect_transito_semana_tipica,   INTERVALS["semanaTipica"],       "transito_semana_tipica"),
            (self.collect_transito_mensual_current, INTERVALS["movilidad_transito"], "transito_mensual_current"),
            (self.collect_tp_daily,                 INTERVALS["movilidad_tp"],       "tp_daily"),
            (self.collect_tp_semana_tipica,         INTERVALS["semanaTipica"],       "tp_semana_tipica"),
            (self.collect_tp_mensual_current,       INTERVALS["movilidad_tp"],       "tp_mensual_current"),
        ]
        threads = []
        for fn, interval, name in jobs:
            t = threading.Thread(target=self.run_loop, args=(fn, interval, name), daemon=True, name=name)
            t.start()
            threads.append(t)
        return threads
