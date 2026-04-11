#!/usr/bin/env python3
"""
Buenos Aires Transport API — data collector
==========================================
Collects data from all /colectivos, /datos, and /transito endpoints
and stores it under ./data/ mirroring the API path structure.

Usage:
    python collect.py [--duration HOURS]

    --duration   Hours to run (default: 2; 0 = run until Ctrl+C)

Credentials in .env (copy .env.example → .env and fill in):
    CLIENT_ID=...
    CLIENT_SECRET=...
"""
import argparse
import logging
import signal
import sys
import time

from config import CLIENT_ID, DATA_DIR
from collectors.colectivos import ColectivosCollector
from collectors.datos import DatosCollector
from collectors.transito import TransitoCollector

# ------------------------------------------------------------------ #
#  Logging                                                             #
# ------------------------------------------------------------------ #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(DATA_DIR.parent / "collector.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("collect")


def check_credentials():
    if not CLIENT_ID:
        logger.error(
            "CLIENT_ID not set. Copy .env.example → .env and add your credentials.\n"
            "Register at: https://api-transporte.buenosaires.gob.ar/registro"
        )
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="BA Transport API collector")
    parser.add_argument("--duration", type=float, default=2.0,
                        help="Hours to collect data (0 = until Ctrl+C)")
    args = parser.parse_args()

    check_credentials()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("=== BA Transport Collector starting ===")
    logger.info("Data directory: %s", DATA_DIR)
    logger.info("Duration: %s", f"{args.duration}h" if args.duration else "until Ctrl+C")

    collectors = [
        ColectivosCollector(),
        DatosCollector(),
        TransitoCollector(),
    ]

    all_threads = []
    for c in collectors:
        all_threads.extend(c.start())

    logger.info("Active threads: %d", len(all_threads))

    end_time = (time.monotonic() + args.duration * 3600) if args.duration > 0 else None

    def shutdown(signum=None, frame=None):
        logger.info("Stopping all collectors...")
        for c in collectors:
            c.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while any(t.is_alive() for t in all_threads):
            if end_time and time.monotonic() >= end_time:
                logger.info("Duration reached. Stopping...")
                shutdown()
                break
            time.sleep(5)
    except KeyboardInterrupt:
        shutdown()

    for t in all_threads:
        t.join(timeout=10)

    logger.info("=== Collection complete. Data saved to: %s ===", DATA_DIR)


if __name__ == "__main__":
    main()
