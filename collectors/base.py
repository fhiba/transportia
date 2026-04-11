"""Base collector: HTTP session, auth params, retry logic."""
import logging
import time
import threading
import requests
from config import CLIENT_ID, CLIENT_SECRET, BASE_URL

logger = logging.getLogger(__name__)


class BaseCollector:
    MAX_RETRIES = 3
    RETRY_BACKOFF = 5  # seconds between retries

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Accept-Encoding": "gzip"})
        self._stop = threading.Event()

    def auth(self) -> dict:
        return {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}

    def get(self, path: str, params: dict | None = None, **kwargs) -> requests.Response | None:
        url = f"{BASE_URL}{path}"
        p = {**(params or {}), **self.auth()}
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                r = self.session.get(url, params=p, timeout=30, **kwargs)
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                logger.warning("[%s] attempt %d/%d failed: %s", path, attempt, self.MAX_RETRIES, e)
                if attempt < self.MAX_RETRIES:
                    time.sleep(self.RETRY_BACKOFF * attempt)
        return None

    def run_loop(self, collect_fn, interval: int, name: str):
        """Run collect_fn every `interval` seconds until stop() called."""
        logger.info("Starting collector: %s (interval=%ds)", name, interval)
        while not self._stop.is_set():
            start = time.monotonic()
            try:
                collect_fn()
            except Exception as e:
                logger.error("[%s] unhandled error: %s", name, e, exc_info=True)
            elapsed = time.monotonic() - start
            sleep_for = max(0, interval - elapsed)
            self._stop.wait(sleep_for)
        logger.info("Stopped collector: %s", name)

    def stop(self):
        self._stop.set()
