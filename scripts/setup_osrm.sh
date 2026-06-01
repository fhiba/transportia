#!/bin/bash
# Setup OSRM local routing server for Transportia
# Downloads Argentina map from Geofabrik and processes it

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
OSRM_DATA_DIR="$PROJECT_ROOT/data/osrm"

mkdir -p "$OSRM_DATA_DIR"

PBF_FILE="$OSRM_DATA_DIR/argentina-latest.osm.pbf"

if [ ! -f "$PBF_FILE" ]; then
    echo "Downloading Argentina map from Geofabrik (~700MB)..."
    curl -L -o "$PBF_FILE" "https://download.geofabrik.de/south-america/argentina-latest.osm.pbf"
    echo "Download complete."
else
    echo "PBF file already exists: $PBF_FILE"
fi

echo "Processing map with OSRM (this takes ~10-20 minutes and ~4GB RAM)..."
docker run --rm -t -v "$OSRM_DATA_DIR":/data osrm/osrm-backend:latest osrm-extract -p /opt/car.lua /data/argentina-latest.osm.pbf
docker run --rm -t -v "$OSRM_DATA_DIR":/data osrm/osrm-backend:latest osrm-partition /data/argentina-latest.osrm
docker run --rm -t -v "$OSRM_DATA_DIR":/data osrm/osrm-backend:latest osrm-customize /data/argentina-latest.osrm

echo ""
echo "Setup complete! Start the server with:"
echo "  docker compose -f docker-compose.osrm.yml up -d"
echo ""
echo "Test it:"
echo "  curl 'http://localhost:5000/route/v1/driving/-58.38,-34.60;-58.37,-34.61?overview=full'"
