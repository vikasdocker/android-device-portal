"""Environment + filesystem configuration for the portal."""
import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
AGENT_URL = os.environ.get("AGENT_URL", "http://host.docker.internal:8765").rstrip("/")
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "change-me")
HOST_DATA_DIR = os.environ.get("HOST_DATA_DIR", "").rstrip("\\/")

DB_PATH = DATA_DIR / "portal.db"
RUNS_DIR = DATA_DIR / "runs"
UPLOADS_DIR = DATA_DIR / "uploads"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def ensure_dirs() -> None:
    for folder in (DATA_DIR, RUNS_DIR, UPLOADS_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def load_devices() -> list[dict]:
    path = CONFIG_DIR / "devices.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("devices", [])


def resolve_run_dir(stored: str | None) -> Path:
    """Map a stored run_dir to a real local path.

    Runs recorded by the Docker portal store container paths ("/data/runs/..."),
    while the no-Docker portal stores a host path. Resolve both, so run history
    and screenshots keep working whichever mode produced the row.
    """
    if not stored:
        return Path()
    path = Path(stored)
    if path.exists():
        return path
    normalized = stored.replace("\\", "/")
    for prefix in ("/data/", "data/"):
        if normalized.startswith(prefix):
            return DATA_DIR / normalized[len(prefix):]
    return path
