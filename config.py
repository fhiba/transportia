import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")

BASE_URL = "https://apitransporte.buenosaires.gob.ar"

# Location for geo-based endpoints
CENTER_LAT = float(os.getenv("CENTER_LAT", "-34.6037"))
CENTER_LON = float(os.getenv("CENTER_LON", "-58.3816"))
RADIUS = int(os.getenv("RADIUS", "2000"))

DATA_DIR = Path(__file__).parent / "data"

# Polling intervals in seconds
INTERVALS = {
    "vehicle_positions":      30,    # GTFS-RT updates every 30s
    "vehicle_positions_simple": 30,
    "service_alerts":         120,
    "cortes":                 300,
    "semaforos":              300,
    "estacionamientos":       600,
    "eventos":                900,
    "movilidad_transito":     3600,
    "movilidad_tp":           3600,
    "semanaTipica":           86400,
    "gtfs_static":            21600,  # 6 hours — schedule rarely changes
}

# Providers for /transito/v1/eventos
EVENTO_PROVIDERS = [1, 201, 769]
