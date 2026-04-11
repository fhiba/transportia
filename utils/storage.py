"""Handles writing collected data to disk, mirroring API endpoint structure."""
import json
import zipfile
import io
from datetime import datetime, timezone
from pathlib import Path
from config import DATA_DIR


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_json(endpoint_path: str, data: dict | list) -> Path:
    """Save dict/list as timestamped JSON under data/{endpoint_path}/."""
    dest = _ensure(DATA_DIR / endpoint_path)
    out = dest / f"{_ts()}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def save_csv(endpoint_path: str, content: str | bytes, filename: str | None = None) -> Path:
    """Save CSV content. filename overrides timestamp (for dated files like mensual)."""
    dest = _ensure(DATA_DIR / endpoint_path)
    name = filename or f"{_ts()}.csv"
    out = dest / name
    if isinstance(content, bytes):
        out.write_bytes(content)
    else:
        out.write_text(content, encoding="utf-8")
    return out


def save_gtfs_zip(endpoint_path: str, data: bytes) -> Path:
    """Save GTFS static zip and extract CSVs alongside it."""
    dest = _ensure(DATA_DIR / endpoint_path)
    ts = _ts()
    zip_out = dest / f"{ts}.zip"
    zip_out.write_bytes(data)

    # Extract CSVs into a subfolder named by timestamp
    try:
        extract_dir = dest / ts
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(extract_dir)
    except zipfile.BadZipFile:
        # Not a zip — might be protobuf; save raw bytes too
        (dest / f"{ts}.pb").write_bytes(data)

    return zip_out


def save_raw(endpoint_path: str, data: bytes, ext: str = "pb") -> Path:
    dest = _ensure(DATA_DIR / endpoint_path)
    out = dest / f"{_ts()}.{ext}"
    out.write_bytes(data)
    return out
