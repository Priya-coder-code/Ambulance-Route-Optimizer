import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from aro.config import Config  # noqa: E402

TEST_DB = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg://aro:aro@localhost:5432/aro_test")
SAMPLE = BACKEND / "sample_data" / "sample_town.osm"

HOSPITALS = [
    {"name": "City General Hospital", "lat": 60.5370, "lon": 26.9420, "osm_id": "node/1"},
    {"name": "Heart Institute", "lat": 60.5235, "lon": 26.9620, "osm_id": "node/2"},
    {"name": "Eye Clinic Hospital", "lat": 60.5300, "lon": 26.9520, "osm_id": "node/3"},
    {"name": "AIIMS Trauma Centre", "lat": 60.5260, "lon": 26.9380, "osm_id": "node/4"},
    {"name": "Women and Children Hospital", "lat": 60.5335, "lon": 26.9660, "osm_id": "node/5"},
]


class TestConfig(Config):
    DATABASE_URL = TEST_DB
    ROAD_GRAPH_OSM_FILE = str(SAMPLE)
    AREA_CENTER = (60.53, 26.95)
    AREA_SIZE_KM = 3
    AREA_NAME = "Sample town"
    SEED_AMBULANCES = 10


@pytest.fixture(scope="session")
def cfg(tmp_path_factory):
    TestConfig.CACHE_DIR = tmp_path_factory.mktemp("cache")
    return TestConfig


@pytest.fixture(scope="session")
def net(cfg):
    from aro.road_network import RoadNetwork

    return RoadNetwork.load(cfg, log=lambda *_: None)


def db_available():
    try:
        import psycopg

        psycopg.connect(TEST_DB.replace("+psycopg", ""), connect_timeout=3).close()
        return True
    except Exception:
        return False
