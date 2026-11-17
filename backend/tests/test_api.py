"""End-to-end API tests on a real PostgreSQL database (aro_test)."""

import pytest

from conftest import HOSPITALS, db_available

pytestmark = pytest.mark.skipif(not db_available(), reason="PostgreSQL test database not reachable")


@pytest.fixture(scope="module")
def client(cfg, net):
    from aro import geocoding
    from aro.app import create_app
    from aro.db import drop_all, init_engine

    init_engine(cfg.DATABASE_URL)
    drop_all()
    geocoding.reverse = lambda c, lat, lon: "Test street"
    app = create_app(cfg, net=net, hospitals=HOSPITALS, background=False)
    return app.test_client()


def test_status_and_seed(client):
    st = client.get("/api/status").get_json()
    assert st["status"] == "ready"
    assert len(client.get("/api/hospitals").get_json()) == len(HOSPITALS)
    assert len(client.get("/api/ambulances").get_json()) == 10
    types = client.get("/api/meta").get_json()["emergency_types"]
    assert {t["code"] for t in types} >= {"accident", "heart", "burn"}


def test_full_dispatch_cycle(client):
    r = client.post("/api/plan", json={"lat": 60.531, "lon": 26.958, "emergency_type": "general",
                                       "severity": "serious", "patient_name": "Test", "address": "Market"})
    assert r.status_code == 200, r.get_json()
    plan = r.get_json()
    did, amb_id, hosp_id = plan["dispatch_id"], plan["ambulance"]["id"], plan["hospital"]["id"]
    assert plan["status"] == "planned" and plan["ambulance"]["route"] and plan["hospital"]["route"]

    # re-planning the same case updates it instead of creating a new one
    again = client.post("/api/plan", json={"lat": 60.531, "lon": 26.958, "emergency_type": "general",
                                           "dispatch_id": did}).get_json()
    assert again["dispatch_id"] == did

    beds = next(h for h in client.get("/api/hospitals").get_json() if h["id"] == hosp_id)["beds_available"]
    d = client.post(f"/api/dispatches/{did}/confirm").get_json()
    assert d["status"] == "dispatched"
    amb = next(a for a in client.get("/api/ambulances").get_json() if a["id"] == amb_id)
    assert amb["status"] == "busy"
    assert next(h for h in client.get("/api/hospitals").get_json() if h["id"] == hosp_id)["beds_available"] == beds - 1

    # a busy ambulance is never chosen again
    p2 = client.post("/api/plan", json={"lat": 60.531, "lon": 26.958, "emergency_type": "general"}).get_json()
    assert p2["ambulance"]["id"] != amb_id

    d = client.post(f"/api/dispatches/{did}/complete").get_json()
    assert d["status"] == "completed"
    amb = next(a for a in client.get("/api/ambulances").get_json() if a["id"] == amb_id)
    assert amb["status"] == "available"
    assert (amb["lat"], amb["lon"]) == (d["hospital"]["lat"], d["hospital"]["lon"])  # parked at the hospital

    hist = client.get("/api/dispatches").get_json()
    assert hist[0]["id"] >= did
    full = client.get(f"/api/dispatches/{did}").get_json()
    assert full["details"]["candidates"]["ambulances"]


def test_cancel_restores_resources(client):
    p = client.post("/api/plan", json={"lat": 60.528, "lon": 26.945, "emergency_type": "general"}).get_json()
    client.post(f"/api/dispatches/{p['dispatch_id']}/confirm")
    d = client.post(f"/api/dispatches/{p['dispatch_id']}/cancel").get_json()
    assert d["status"] == "cancelled" and d["ambulance"]["status"] == "available"


def test_road_conditions_and_traffic(client):
    r = client.post("/api/road-conditions", json={"kind": "closure", "lat": 60.531, "lon": 26.955})
    assert r.status_code == 201
    j = r.get_json()
    assert j["effect"]["closed_edges"] > 0 and j["shapes"]["closed"]
    cid = j["conditions"][-1]["id"]
    j = client.delete(f"/api/road-conditions/{cid}").get_json()
    assert j["effect"]["closed_edges"] == 0
    assert client.post("/api/traffic-profile", json={"profile": "peak"}).status_code == 200
    client.post("/api/traffic-profile", json={"profile": "normal"})


def test_validation(client):
    assert client.post("/api/plan", json={"emergency_type": "general"}).status_code == 400
    assert client.post("/api/plan", json={"lat": 60.53, "lon": 26.95}).status_code == 400
    assert client.post("/api/plan", json={"lat": 0, "lon": 0, "emergency_type": "general"}).status_code == 422
