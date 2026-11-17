import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aro.config import Config  # noqa: E402

TEST_DB = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg://aro:aro@localhost:5432/aro_test")
SAMPLE = ROOT / "sample_data" / "sample_town.osm"

HOSPITALS = [
    {"kind": "hospital", "name": "City General Hospital", "lat": 60.5370, "lon": 26.9420, "osm_id": "node/1"},
    {"kind": "hospital", "name": "Heart Institute", "lat": 60.5235, "lon": 26.9620, "osm_id": "node/2"},
    {"kind": "hospital", "name": "Eye Clinic Hospital", "lat": 60.5300, "lon": 26.9520, "osm_id": "node/3"},
    {"kind": "hospital", "name": "AIIMS Trauma Centre", "lat": 60.5260, "lon": 26.9380, "osm_id": "node/4"},
    {"kind": "hospital", "name": "Women and Children Hospital", "lat": 60.5335, "lon": 26.9660, "osm_id": "node/5"},
]


class TestConfig(Config):
    DATABASE_URL = TEST_DB
    DB_MODE = "postgresql"
    SERVERLESS = False
    AREA_CENTER = (60.53, 26.95)
    AREA_SIZE_KM = 3
    AREA_NAME = "Sample town"
    SEED_AMBULANCES = 10


def sample_osm():
    """Real OpenStreetMap extract (small town) + test hospitals, in the format osm.py produces."""
    from aro.osm import load_osm_xml

    osm = load_osm_xml(SAMPLE)
    osm["pois"] = [dict(h) for h in HOSPITALS]
    return osm


@pytest.fixture(scope="session")
def cfg():
    return TestConfig


@pytest.fixture(scope="session")
def net():
    from aro.road_network import RoadNetwork

    return RoadNetwork.from_osm(sample_osm(), "Sample town")


def db_available():
    try:
        import psycopg

        psycopg.connect(TEST_DB.replace("+psycopg", ""), connect_timeout=3).close()
        return True
    except Exception:
        return False
