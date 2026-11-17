"""All settings come from environment variables (or a .env file in the project root)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:  # optional: load .env
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("SERVERLESS"))


def database_url() -> tuple[str, str]:
    """(SQLAlchemy URL, mode).

    mode = "postgresql"        DATABASE_URL is set (local PostgreSQL, or Neon on Vercel)
           "sqlite-local"      no DATABASE_URL, running on your computer -> local.db file
           "sqlite-temporary"  no DATABASE_URL on Vercel -> /tmp, data is lost on restarts (demo mode)
    """
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or os.getenv("DATABASE_POSTGRES_URL")
    if url:
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+psycopg://" + url[len(prefix):]
        return url, "postgresql"
    if SERVERLESS:
        return "sqlite:////tmp/aro.db", "sqlite-temporary"
    return f"sqlite:///{ROOT / 'local.db'}", "sqlite-local"


def _floats(text: str) -> tuple[float, float]:
    a, b = text.split(",")
    return float(a), float(b)


class Config:
    DATABASE_URL, DB_MODE = database_url()
    SERVERLESS = SERVERLESS

    # Service area: a square of AREA_SIZE_KM x AREA_SIZE_KM around AREA_CENTER
    AREA_NAME = os.getenv("AREA_NAME", "Central Delhi")
    AREA_CENTER = _floats(os.getenv("AREA_CENTER", "28.6315,77.2167"))  # Connaught Place
    AREA_SIZE_KM = float(os.getenv("AREA_SIZE_KM", "5"))

    # Offline / tests: read the roads from a .osm file instead of downloading them
    ROAD_GRAPH_OSM_FILE = os.getenv("ROAD_GRAPH_OSM_FILE", "")

    # Nominatim (OpenStreetMap geocoding)
    NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
    USER_AGENT = os.getenv("USER_AGENT", "AmbulanceRouteOptimizer/3.0 (student ADA project)")

    # Simulation / planner
    SEED_AMBULANCES = int(os.getenv("SEED_AMBULANCES", "15"))
    HOSPITAL_CANDIDATES = int(os.getenv("HOSPITAL_CANDIDATES", "5"))
    RANDOM_SEED = int(os.getenv("RANDOM_SEED", "42"))

    # The built React app (frontend/ -> npm run build -> public/)
    PUBLIC_DIR = ROOT / "public"
