"""Dijkstra correctness and the dispatch decision, checked against brute force."""

import math
import random

import pytest

from aro.dijkstra import bellman_ford, dijkstra
from aro.planner import PlanError, PlanRequest, plan_dispatch
from aro.seed import EMERGENCY_TYPES, simulate_facilities
from conftest import HOSPITALS


def random_graph(n=80, extra=200, seed=0):
    rng = random.Random(seed)
    adj = [[] for _ in range(n)]
    edges, w = [], []
    order = list(range(n))
    rng.shuffle(order)
    pairs = list(zip(order, order[1:] + order[:1]))
    pairs += [(rng.randrange(n), rng.randrange(n)) for _ in range(extra)]
    for u, v in pairs:
        if u != v:
            adj[u].append((v, len(edges)))
            edges.append((u, v))
            w.append(rng.uniform(1, 100))
    return adj, edges, w


@pytest.mark.parametrize("seed", range(5))
def test_dijkstra_matches_bellman_ford(seed):
    adj, edges, w = random_graph(seed=seed)
    ref = bellman_ford(len(adj), edges, w, 0)
    full = dijkstra(adj, w, 0, targets=None)
    assert full.settled == len(adj)
    for t in range(len(adj)):
        assert full.dist[t] == pytest.approx(ref[t])
        r = dijkstra(adj, w, 0, targets={t})
        assert r.dist[t] == pytest.approx(ref[t])
        nodes, path_edges = r.path_to(t)
        assert nodes[0] == 0 and nodes[-1] == t
        assert sum(w[e] for e in path_edges) == pytest.approx(ref[t])


def test_cutoff_and_stop_condition():
    adj, edges, w = random_graph(seed=9)
    ref = bellman_ford(len(adj), edges, w, 0)
    far = max(range(len(adj)), key=lambda i: ref[i])
    r = dijkstra(adj, w, 0, targets={far}, cutoff=ref[far] / 2)
    assert not r.reached and r.stopped_by_cutoff
    assert all(d <= ref[far] / 2 for d in r.dist.values())
    # K nearest targets, in order of distance
    targets = set(random.Random(1).sample(range(1, len(adj)), 10))
    seen = []
    r = dijkstra(adj, w, 0, targets=targets, stop_condition=lambda n: (seen.append(n) or len(seen) >= 3))
    assert r.reached == sorted(targets, key=lambda t: ref[t])[:3]


def test_closed_road_is_avoided():
    adj, edges, w = random_graph(seed=4)
    r = dijkstra(adj, w, 0, targets={5})
    _, path = r.path_to(5)
    w2 = list(w)
    w2[path[0]] = math.inf
    r2 = dijkstra(adj, w2, 0, targets={5})
    if r2.reached:
        assert path[0] not in r2.path_to(5)[1]
        assert r2.dist[5] >= r.dist[5]


# ---------------------------------------------------------------- real roads
def test_network_is_strongly_connected(net):
    full = dijkstra(net.adj, net.weights(), 0, targets=None)
    assert full.settled == net.n
    radj = [[] for _ in range(net.n)]
    for eid, (u, v) in enumerate(zip(net.e_u, net.e_v)):
        radj[v].append((u, eid))
    assert dijkstra(radj, net.weights(), 0, targets=None).settled == net.n


def test_route_geometry(net):
    r = dijkstra(net.adj, net.weights(), 0, targets={net.n - 1})
    _, edges = r.path_to(net.n - 1)
    pts = net.route_coords(edges)
    assert pts[0] == pytest.approx([net.lat[0], net.lon[0]], abs=1e-5)
    assert pts[-1] == pytest.approx([net.lat[net.n - 1], net.lon[net.n - 1]], abs=1e-5)
    assert net.route_summary(edges)["time_s"] == pytest.approx(r.dist[net.n - 1])


def make_world(net, seed):
    rng = random.Random(seed)
    s, w, n, e = net.bounds()
    ambulances = [{"id": i, "code": f"A{i}", "kind": rng.choice(["ALS", "BLS"]), "base_name": "",
                   "lat": rng.uniform(s, n), "lon": rng.uniform(w, e),
                   "status": rng.choice(["available"] * 4 + ["busy"])} for i in range(1, 13)]
    hospitals = []
    for i, h in enumerate(HOSPITALS, 1):
        fac, beds = simulate_facilities(h["name"], f"{seed}-{i}", seed)
        hospitals.append({**h, "id": i, "facilities": fac, "beds_available": beds, "is_active": True})
    return ambulances, hospitals


def brute_force_times(net, src_nodes, dst):
    """Full single-source Dijkstra from every source: the slow, obviously-correct way."""
    w = net.weights()
    return {s: dijkstra(net.adj, w, s, targets=None).dist.get(dst, math.inf) for s in src_nodes}


@pytest.mark.parametrize("seed", range(12))
def test_decision_is_optimal(net, seed):
    """Selected ambulance and hospital must be the true minimum (pruning must never change the answer)."""
    rng = random.Random(100 + seed)
    ambulances, hospitals = make_world(net, seed)
    et_row = rng.choice(EMERGENCY_TYPES)
    et = {"code": et_row[0], "label": et_row[1], "required_facilities": et_row[3], "needs_als": et_row[4]}
    severity = rng.choice(["critical", "serious", "stable"])
    s, w, n, e = net.bounds()
    lat, lon = rng.uniform(s, n), rng.uniform(w, e)
    try:
        res = plan_dispatch(net, PlanRequest(lat, lon, et, severity, show_search=False), ambulances, hospitals)
    except PlanError:
        pytest.skip("no ambulance free in this random world")
    patient, _ = net.nearest_open_node(lat, lon)

    # ambulance: minimum over the eligible pool
    free = [a for a in ambulances if a["status"] == "available"]
    pool = [a for a in free if a["kind"] == "ALS"] if (et["needs_als"] or severity == "critical") else free
    pool = pool or free
    nodes = {a["id"]: net.nearest_open_node(a["lat"], a["lon"])[0] for a in pool}
    times = brute_force_times(net, set(nodes.values()), patient)
    best = min(times[nodes[a["id"]]] for a in pool)
    assert res["ambulance"]["eta_s"] == pytest.approx(best)
    assert res["ambulance"]["id"] in {a["id"] for a in pool}

    # hospital: minimum over suitable hospitals (unless the fallback rule was used)
    if not res["candidates"]["fallback_hospital_rule"]:
        assert set(et["required_facilities"]) <= set(res["hospital"]["facilities"])
        suitable = [h for h in hospitals if set(et["required_facilities"]) <= set(h["facilities"])
                    and h["beds_available"] > 0]
        w_ = net.weights()
        from_patient = dijkstra(net.adj, w_, patient, targets=None).dist
        best_h = min(from_patient[net.nearest_open_node(h["lat"], h["lon"])[0]] for h in suitable)
        assert res["hospital"]["eta_s"] == pytest.approx(best_h)
    assert res["total"]["eta_s"] == pytest.approx(res["ambulance"]["eta_s"] + res["hospital"]["eta_s"])


def test_unsuitable_nearer_hospital_is_skipped(net):
    ambulances, hospitals = make_world(net, 1)
    for a in ambulances:
        a["status"], a["kind"] = "available", "ALS"
    for h in hospitals:
        h["facilities"], h["beds_available"] = ["emergency"], 5
    burn_h = hospitals[-1]
    burn_h["facilities"] = ["emergency", "burn"]
    et = {"code": "burn", "label": "Burn", "required_facilities": ["emergency", "burn"], "needs_als": True}
    res = plan_dispatch(net, PlanRequest(burn_h["lat"] - 0.008, burn_h["lon"] - 0.02, et), ambulances, hospitals)
    assert res["hospital"]["id"] == burn_h["id"]
    for row in res["candidates"]["skipped_hospitals"]:
        assert "burn" in row["reason"]


def test_traffic_and_closures_change_times(net):
    ambulances, hospitals = make_world(net, 3)
    et = {"code": "general", "label": "General", "required_facilities": ["emergency"], "needs_als": False}
    for h in hospitals:
        h["facilities"] = ["emergency"]
        h["beds_available"] = 3
    lat, lon = 60.531, 26.955
    net.set_profile("free")
    t_free = plan_dispatch(net, PlanRequest(lat, lon, et), ambulances, hospitals)["total"]["eta_s"]
    net.set_profile("peak")
    t_peak = plan_dispatch(net, PlanRequest(lat, lon, et), ambulances, hospitals)["total"]["eta_s"]
    net.set_profile("normal")
    assert t_peak > t_free
    res = plan_dispatch(net, PlanRequest(lat, lon, et), ambulances, hospitals)
    route = res["hospital"]["route"]
    mid = route[len(route) // 2]
    net.apply_conditions([{"kind": "closure", "lat": mid[0], "lon": mid[1], "radius_m": 15, "factor": 1, "active": True}])
    try:
        res2 = plan_dispatch(net, PlanRequest(lat, lon, et), ambulances, hospitals)
        assert res2["total"]["eta_s"] >= res["total"]["eta_s"] - 1e-6
    finally:
        net.apply_conditions([])


def test_simulated_facilities_are_deterministic():
    assert simulate_facilities("Some Hospital", "node/9") == simulate_facilities("Some Hospital", "node/9")
    assert simulate_facilities("AIIMS New Delhi", "x")[0][0] == "emergency"
    assert simulate_facilities("City Eye Hospital", "y")[0] == []
