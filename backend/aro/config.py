"""All settings come from environment variables (or backend/.env)."""

from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

try:  # optional: load backend/.env
    from dotenv import load_dotenv

    load_dotenv(BACKEND_DIR / ".env")
except ImportError:
    pass


def database_url() -> str:
    """DATABASE_URL (local .env, or set automatically by the Neon integration on Vercel).
    Neon/Vercel give 'postgres://' or 'postgresql://' URLs; SQLAlchemy needs the driver name."""
    url = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
           or "postgresql+psycopg://aro:aro@localhost:5432/aro")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
    return url


def _floats(text: str) -> tuple[float, float]:
    a, b = text.split(",")
    return float(a), float(b)


class Config:
    # PostgreSQL connection
    DATABASE_URL = database_url()

    # Running as a serverless function on Vercel (Vercel sets VERCEL=1).
    # Then: no OSMnx download, the pre-built road graph file is loaded instead,
    # and data seeding must be done beforehand with manage.py.
    SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("SERVERLESS"))

    # Service area: a square of AREA_SIZE_KM x AREA_SIZE_KM around AREA_CENTER
    AREA_NAME = os.getenv("AREA_NAME", "Central Delhi")
    AREA_CENTER = _floats(os.getenv("AREA_CENTER", "28.6315,77.2167"))  # Connaught Place
    AREA_SIZE_KM = float(os.getenv("AREA_SIZE_KM", "50"))

    # Offline alternative: build the road graph from a local .osm file instead
    ROAD_GRAPH_OSM_FILE = os.getenv("ROAD_GRAPH_OSM_FILE", "")

    CACHE_DIR = Path(os.getenv("CACHE_DIR", str(BACKEND_DIR / "data" / "cache")))

    # Pre-built road graph shipped with the deployment (made by: python manage.py export-graph)
    ROAD_NETWORK_FILE = Path(os.getenv("ROAD_NETWORK_FILE", str(BACKEND_DIR / "deploy" / "roadnet.pkl.gz")))

    # Nominatim (OpenStreetMap geocoding)
    NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
    USER_AGENT = os.getenv("USER_AGENT", "AmbulanceRouteOptimizer/2.0 (student ADA project)")

    # Simulation / planner settings
    SEED_AMBULANCES = int(os.getenv("SEED_AMBULANCES", "30"))
    HOSPITAL_CANDIDATES = int(os.getenv("HOSPITAL_CANDIDATES", "5"))
    RANDOM_SEED = int(os.getenv("RANDOM_SEED", "42"))

    # Serve the built React app (frontend/dist) from Flask
    FRONTEND_DIST = Path(os.getenv("FRONTEND_DIST", str(BACKEND_DIR.parent / "frontend" / "dist")))
