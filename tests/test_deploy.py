"""How the app behaves when deployed (Vercel): first start, cache, demo mode, failures.

OpenStreetMap is replaced by a fake that returns real OSM data (the sample town)."""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from conftest import HOSPITALS, ROOT, SAMPLE, TestConfig, db_available


def overpass_json():
    """The sample .osm file converted to what the Overpass API returns."""
    root = ET.parse(SAMPLE).getroot()
    els = [{"type": "node", "id": int(n.get("id")), "lat": float(n.get("lat")), "lon": float(n.get("lon"))}
           for n in root.iter("node")]
    for w in root.iter("way"):
        els.append({"type": "way", "id": int(w.get("id")), "nodes": [int(nd.get("ref")) for nd in w.iter("nd")],
                    "tags": {t.get("k"): t.get("v") for t in w.iter("tag")}})
    for i, h in enumerate(HOSPITALS, 1):   # hospitals: some as nodes, some as building outlines (ways)
        if i % 2:
            els.append({"type": "node", "id": 9000 + i, "lat": h["lat"], "lon": h["lon"],
                        "tags": {"amenity": "hospital", "name": h["name"]}})
        else:
            els.append({"type": "way", "id": 9000 + i, "center": {"lat": h["lat"], "lon": h["lon"]},
                        "nodes": [1, 2], "tags": {"amenity": "hospital", "name": h["name"]}})
    els.append({"type": "node", "id": 9999, "lat": 60.53, "lon": 26.95, "tags": {"amenity": "hospital"}})  # no name
    els.append({"type": "way", "id": 8888, "nodes": [1, 2], "tags": {"highway": "footway"}})
    return {"elements": els}


def test_parse_overpass():
    from aro.osm import parse_overpass

    osm = parse_overpass(overpass_json())
    assert len(osm["pois"]) == len(HOSPITALS)                 # unnamed one skipped
    assert all(p["kind"] == "hospital" for p in osm["pois"])
    assert not any(w[0] == 8888 for w in osm["ways"])          # footway is not drivable
    assert len(osm["ways"]) > 50 and len(osm["nodes"]) > 500


def test_database_url_forms(monkeypatch):
    import importlib

    from aro import config

    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@ep-x-pooler.neon.tech/db?sslmode=require")
    url, mode = config.database_url()
    assert url.startswith("postgresql+psycopg://u:p@ep-x") and mode == "postgresql"
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.setattr(config, "SERVERLESS", True)
    assert config.database_url() == ("sqlite:////tmp/aro.db", "sqlite-temporary")
    importlib.reload(config)


def make_cfg(url, mode):
    class C(TestConfig):
        DATABASE_URL, DB_MODE, SERVERLESS = url, mode, True
    return C


@pytest.fixture
def fake_osm(monkeypatch):
    """Count downloads; return real data."""
    from aro import server
    from aro.osm import parse_overpass

    calls = {"n": 0, "fail": False}

    def fake_download(bbox, log=print):
        calls["n"] += 1
        if calls["fail"]:
            raise RuntimeError("Could not download OpenStreetMap data: server busy (HTTP 504)")
        return parse_overpass(overpass_json())

    monkeypatch.setattr(server, "download", fake_download)
    monkeypatch.setattr(server, "RETRY_SECONDS", 0)
    from aro import geocoding
    monkeypatch.setattr(geocoding, "reverse", lambda c, lat, lon: "Test street")
    return calls


def run_first_start(cfg, fake_osm):
    from aro.db import drop_all, init_engine
    from aro.server import create_app

    init_engine(cfg.DATABASE_URL)
    drop_all()
    # instance A: empty database -> downloads, builds, seeds
    a = create_app(cfg).test_client()
    st = a.get("/api/status").get_json()
    assert st["status"] == "ready", st
    assert fake_osm["n"] == 1
    hospitals = a.get("/api/hospitals").get_json()
    assert {h["name"] for h in hospitals} == {h["name"] for h in HOSPITALS}
    assert len(a.get("/api/ambulances").get_json()) == cfg.SEED_AMBULANCES

    # instance B (another Vercel copy / a cold start): map comes from the database, no download
    b = create_app(cfg).test_client()
    assert b.get("/api/status").get_json()["status"] == "ready"
    assert fake_osm["n"] == 1
    assert len(b.get("/api/hospitals").get_json()) == len(HOSPITALS)          # not seeded twice

    # full case on B, traffic changed on A is used by B
    plan = {"lat": 60.531, "lon": 26.958, "emergency_type": "general"}
    t1 = b.post("/api/plan", json=plan).get_json()["total"]["eta_s"]
    a.post("/api/traffic-profile", json={"profile": "peak"})
    r = b.post("/api/plan", json=plan)
    assert r.status_code == 200 and r.get_json()["total"]["eta_s"] > t1
    did = r.get_json()["dispatch_id"]
    assert a.post(f"/api/dispatches/{did}/confirm").get_json()["status"] == "dispatched"
    assert b.post(f"/api/dispatches/{did}/complete").get_json()["status"] == "completed"
    assert a.post("/api/road-conditions", json={"kind": "closure", "lat": 60.531, "lon": 26.955}).status_code == 201
    assert len(b.get("/api/road-conditions").get_json()["shapes"]["closed"]) >= 1
    a.post("/api/traffic-profile", json={"profile": "normal"})
    return a


def test_first_start_sqlite_demo_mode(tmp_path, fake_osm):
    cfg = make_cfg(f"sqlite:///{tmp_path / 'demo.db'}", "sqlite-temporary")
    a = run_first_start(cfg, fake_osm)
    assert a.get("/api/status").get_json()["db_mode"] == "sqlite-temporary"


@pytest.mark.skipif(not db_available(), reason="PostgreSQL test database not reachable")
def test_first_start_postgresql(fake_osm):
    run_first_start(make_cfg(TestConfig.DATABASE_URL, "postgresql"), fake_osm)


def test_download_failure_is_reported_and_retried(tmp_path, fake_osm):
    from aro.server import create_app

    fake_osm["fail"] = True
    c = create_app(make_cfg(f"sqlite:///{tmp_path / 'x.db'}", "sqlite-temporary")).test_client()
    st = c.get("/api/status")
    assert st.status_code == 200 and st.get_json()["status"] == "error"
    assert "OpenStreetMap" in st.get_json()["error"]
    assert c.post("/api/plan", json={"lat": 1, "lon": 1, "emergency_type": "general"}).status_code == 503
    assert c.get("/api/meta").status_code == 200        # database still works
    fake_osm["fail"] = False                             # OpenStreetMap is back
    assert c.get("/api/status").get_json()["status"] == "ready"


def test_bad_database_url_never_crashes():
    from aro.server import create_app

    cfg = make_cfg("postgresql+psycopg://nobody:wrong@127.0.0.1:1/none", "postgresql")
    c = create_app(cfg).test_client()
    st = c.get("/api/status")
    assert st.status_code == 200 and st.get_json()["status"] == "error"
    assert "database" in st.get_json()["error"].lower()
    r = c.get("/api/meta")
    assert r.status_code == 503 and "error" in r.get_json()


def test_vercel_entrypoint_imports_without_database():
    """What Vercel does: import app.py with VERCEL=1 and (maybe) no DATABASE_URL. Must not crash."""
    code = ("import app, json; c = app.app.test_client(); "
            "print(json.dumps({'meta': c.get('/api/meta').status_code, "
            "'mode': c.get('/api/status').get_json()['db_mode']}))")
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "POSTGRES_URL")}
    env.update(VERCEL="1", ROAD_GRAPH_OSM_FILE=str(SAMPLE), AREA_CENTER="60.53,26.95", AREA_SIZE_KM="3")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res == {"meta": 200, "mode": "sqlite-temporary"}
