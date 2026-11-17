"""Unit tests. Run with:  python -m pytest -q   (or  python -m unittest discover tests)"""

import json
import math
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aro.algorithms import astar, bellman_ford, dijkstra  # noqa: E402
from aro.dispatcher import Dispatcher  # noqa: E402
from aro.geo import GridIndex, haversine  # noqa: E402
from aro.graph import RoadGraph, oneway_direction, parse_maxspeed  # noqa: E402
from aro.osm_loader import OSMData, load_osm_xml, parse_overpass_json, split_bbox, square_bbox  # noqa: E402

SAMPLE = ROOT / "sample_data" / "sample_town.osm"


def random_graph(n=60, extra=120, seed=0) -> RoadGraph:
    """Random strongly-connected graph with real coordinates near Delhi."""
    rng = random.Random(seed)
    g = RoadGraph()
    for _ in range(n):
        g.add_node(28.6 + rng.random() * 0.05, 77.2 + rng.random() * 0.05)
    order = list(range(n))
    rng.shuffle(order)
    for a, b in zip(order, order[1:] + order[:1]):          # a ring -> strongly connected
        g.add_edge(a, b, None, rng.choice([20, 30, 50, 60]))
    for _ in range(extra):
        a, b = rng.randrange(n), rng.randrange(n)
        if a != b:
            # length >= straight line (roads are never shorter than the crow flies)
            straight = haversine(g.lat[a], g.lon[a], g.lat[b], g.lon[b])
            g.add_edge(a, b, straight * rng.uniform(1.0, 1.6), rng.choice([20, 30, 50, 60]))
    return g.finalize()


class TestAlgorithms(unittest.TestCase):
    def test_against_bellman_ford(self):
        for seed in range(5):
            g = random_graph(seed=seed)
            for metric in ("time", "distance"):
                ref = bellman_ford(g, 0, metric)
                full = dijkstra(g, 0, None, metric)
                self.assertEqual(full.nodes_explored, g.n)
                for t in range(g.n):
                    d = dijkstra(g, 0, t, metric)
                    a = astar(g, 0, t, metric)
                    self.assertAlmostEqual(d.cost, ref[t], places=6)
                    self.assertAlmostEqual(a.cost, ref[t], places=6)
                    self.assertLessEqual(a.nodes_explored, d.nodes_explored + 1)

    def test_path_is_consistent_with_cost(self):
        g = random_graph(seed=11)
        w = g.weights("time")
        for t in range(1, g.n):
            r = astar(g, 0, t)
            self.assertEqual(r.path[0], 0)
            self.assertEqual(r.path[-1], t)
            self.assertEqual(len(r.path_edges), len(r.path) - 1)
            for (a, b), eid in zip(zip(r.path, r.path[1:]), r.path_edges):
                self.assertEqual((g.edges[eid].u, g.edges[eid].v), (a, b))
            self.assertAlmostEqual(sum(w[e] for e in r.path_edges), r.cost, places=6)

    def test_multi_target_finds_nearest(self):
        g = random_graph(seed=3)
        targets = {5, 17, 33, 48}
        ref = bellman_ford(g, 0)
        best = min(ref[t] for t in targets)
        for algo in (dijkstra, astar):
            r = algo(g, 0, targets)
            self.assertIn(r.target, targets)
            self.assertAlmostEqual(r.cost, best, places=6)

    def test_reverse_search(self):
        g = random_graph(seed=4)
        for algo in (dijkstra, astar):
            r = algo(g, 7, {0}, reverse=True)            # who reaches 7? -> path 0 ... 7 reversed
            fwd = dijkstra(g, 0, 7)
            self.assertAlmostEqual(r.cost, fwd.cost, places=6)

    def test_blocked_and_traffic(self):
        g = random_graph(seed=5)
        before = dijkstra(g, 0, 10).cost
        r = dijkstra(g, 0, 10)
        g.block_edge(r.path_edges[0])
        after = dijkstra(g, 0, 10)
        self.assertGreaterEqual(after.cost, before)
        self.assertNotIn(r.path_edges[0], after.path_edges)
        self.assertAlmostEqual(astar(g, 0, 10).cost, after.cost, places=6)
        g.clear_traffic()
        for eid in range(len(g.edges)):
            g.traffic[eid] = 3.0
        g._changed()
        self.assertAlmostEqual(dijkstra(g, 0, 10).cost, 3 * before, places=5)
        self.assertAlmostEqual(astar(g, 0, 10).cost, 3 * before, places=5)

    def test_unreachable(self):
        g = RoadGraph()
        a, b = g.add_node(28.6, 77.2), g.add_node(28.61, 77.21)
        g.add_edge(a, b)
        g.finalize()
        for algo in (dijkstra, astar):
            r = algo(g, b, a)
            self.assertFalse(r.found)
            self.assertTrue(math.isinf(r.cost))


class TestRealMap(unittest.TestCase):
    """The bundled sample is a real OpenStreetMap extract."""

    @classmethod
    def setUpClass(cls):
        cls.osm = load_osm_xml(SAMPLE)
        cls.g = RoadGraph.from_osm(cls.osm)
        cls.raw = RoadGraph.from_osm(cls.osm, simplify=False)

    def test_graph_is_strongly_connected(self):
        self.assertEqual(len(self.g.strongly_connected_components()), 1)

    def test_simplification_keeps_distances(self):
        pos = {(round(a, 6), round(b, 6)): i for i, (a, b) in enumerate(zip(self.raw.lat, self.raw.lon))}
        to_raw = [pos[(round(a, 6), round(b, 6))] for a, b in zip(self.g.lat, self.g.lon)]
        rng = random.Random(1)
        for _ in range(150):
            s, t = rng.randrange(self.g.n), rng.randrange(self.g.n)
            for metric in ("time", "distance"):
                self.assertAlmostEqual(dijkstra(self.g, s, t, metric).cost,
                                       dijkstra(self.raw, to_raw[s], to_raw[t], metric).cost, places=4)

    def test_dijkstra_equals_astar(self):
        rng = random.Random(2)
        for _ in range(300):
            s, t = rng.randrange(self.g.n), rng.randrange(self.g.n)
            for metric in ("time", "distance"):
                self.assertAlmostEqual(dijkstra(self.g, s, t, metric).cost, astar(self.g, s, t, metric).cost, places=6)

    def test_heuristic_admissible_and_consistent(self):
        """h(u) <= w(u,v) + h(v) on every edge, and h(u) <= true cost."""
        g = self.g
        xs, ys = g.projected_xy()
        from aro.graph import PROJECTION_SAFETY
        for metric in ("time", "distance"):
            w = g.weights(metric)
            sc = g.heuristic_scale(metric) * PROJECTION_SAFETY
            for target in (0, g.n // 2, g.n - 1):
                h = [sc * math.hypot(xs[i] - xs[target], ys[i] - ys[target]) for i in range(g.n)]
                for e_id, e in enumerate(g.edges):
                    self.assertLessEqual(h[e.u], w[e_id] + h[e.v] + 1e-9)
                for s in range(0, g.n, 7):
                    self.assertLessEqual(h[s], dijkstra(g, s, target, metric).cost + 1e-9)

    def test_geometry_starts_and_ends_at_nodes(self):
        r = astar(self.g, 0, self.g.n - 1)
        pts = self.g.path_coords(r.path_edges)
        self.assertAlmostEqual(pts[0][0], self.g.lat[0], places=5)
        self.assertAlmostEqual(pts[-1][1], self.g.lon[self.g.n - 1], places=5)

    def test_block_road_both_directions(self):
        g = RoadGraph.from_osm(self.osm)
        e = g.edges[0]
        mid = e.geom[len(e.geom) // 2] if e.geom else ((g.lat[e.u] + g.lat[e.v]) / 2, (g.lon[e.u] + g.lon[e.v]) / 2)
        closed = g.block_near(mid[0], mid[1], 5)
        self.assertIn(0, closed)
        w = g.weights("time")
        self.assertTrue(all(math.isinf(w[i]) for i in closed))


class TestDispatcher(unittest.TestCase):
    def test_dispatch_on_sample(self):
        osm = load_osm_xml(SAMPLE)
        g = RoadGraph.from_osm(osm)
        pois = json.loads((ROOT / "sample_data" / "sample_town_hospitals.json").read_text())
        d = Dispatcher.from_pois(g, pois)
        self.assertEqual(len(d.hospitals), 2)
        out = d.dispatch(60.531, 26.958, mark_busy=True)
        for k in ("to_patient", "to_hospital"):
            rd, ra = out["legs"]["dijkstra"][k][0], out["legs"]["astar"][k][0]
            self.assertAlmostEqual(rd.cost, ra.cost, places=6)
        # the leg-1 path must end at the incident and start at the ambulance
        r1, amb = out["legs"]["astar"]["to_patient"]
        self.assertEqual(r1.path[0], amb.node)
        self.assertEqual(r1.path[-1], out["incident"]["node"])
        self.assertFalse(amb.available)
        # brute force: the chosen ambulance really is the fastest one
        best = min(dijkstra(g, a.node, out["incident"]["node"]).cost for a in d.ambulances if a is not amb)
        self.assertLessEqual(r1.cost, best + 1e-6)


class TestLoaders(unittest.TestCase):
    def test_overpass_json(self):
        data = {"elements": [
            {"type": "way", "id": 1, "nodes": [10, 11, 12], "tags": {"highway": "primary", "oneway": "yes", "name": "Ring Rd"}},
            {"type": "way", "id": 2, "nodes": [12, 13], "tags": {"highway": "footway"}},
            {"type": "node", "id": 10, "lat": 28.60, "lon": 77.20},
            {"type": "node", "id": 11, "lat": 28.601, "lon": 77.201},
            {"type": "node", "id": 12, "lat": 28.602, "lon": 77.202},
            {"type": "way", "id": 3, "center": {"lat": 28.61, "lon": 77.21}, "nodes": [1, 2],
             "tags": {"amenity": "hospital", "name": "AIIMS"}},
            {"type": "node", "id": 99, "lat": 28.62, "lon": 77.22, "tags": {"emergency": "ambulance_station"}},
        ]}
        t = parse_overpass_json(data)
        self.assertEqual(len(t["ways"]), 1)          # footway dropped
        self.assertEqual(t["ways"][0][1]["name"], "Ring Rd")
        self.assertEqual({p["kind"] for p in t["pois"]}, {"hospital", "ambulance_station"})
        self.assertEqual(t["pois"][0]["name"], "AIIMS")

    def test_tiles_cover_area(self):
        bb = square_bbox(28.6315, 77.2167, 50)
        self.assertAlmostEqual(haversine(bb[0], bb[1], bb[2], bb[1]) / 1000, 50, delta=0.5)
        self.assertAlmostEqual(haversine(bb[0], bb[1], bb[0], bb[3]) / 1000, 50, delta=0.5)
        tiles = split_bbox(bb, 10)
        self.assertEqual(len(tiles), 25)
        self.assertAlmostEqual(min(t[0] for t in tiles), bb[0], places=5)
        self.assertAlmostEqual(max(t[3] for t in tiles), bb[3], places=5)

    def test_tags(self):
        self.assertEqual(parse_maxspeed("50"), 50)
        self.assertAlmostEqual(parse_maxspeed("30 mph"), 48.28, places=1)
        self.assertIsNone(parse_maxspeed("none"))
        self.assertEqual(oneway_direction({"oneway": "yes"}), 1)
        self.assertEqual(oneway_direction({"oneway": "-1"}), -1)
        self.assertEqual(oneway_direction({"junction": "roundabout"}), 1)
        self.assertEqual(oneway_direction({"highway": "residential"}), 0)

    def test_grid_index_exact(self):
        rng = random.Random(9)
        lats = [28.5 + rng.random() * 0.2 for _ in range(2000)]
        lons = [77.1 + rng.random() * 0.2 for _ in range(2000)]
        idx = GridIndex(lats, lons)
        for _ in range(200):
            q = (28.45 + rng.random() * 0.3, 77.05 + rng.random() * 0.3)
            best = min(range(2000), key=lambda i: haversine(q[0], q[1], lats[i], lons[i]))
            self.assertEqual(idx.nearest(*q)[0], best)

    def test_synthetic_osm_build(self):
        osm = OSMData("t", (0, 0, 1, 1), nodes={1: (28.60, 77.20), 2: (28.601, 77.20), 3: (28.602, 77.20),
                                                4: (28.602, 77.201)},
                      ways=[({"highway": "residential"}, [1, 2, 3, 4])])
        g = RoadGraph.from_osm(osm)
        self.assertEqual(g.n, 2)                    # only the two dead ends remain
        self.assertEqual(len(g.edges), 2)
        self.assertEqual(len(g.edges[0].geom), 2)   # the two middle points kept as shape


if __name__ == "__main__":
    unittest.main()


class TestDownloadPipeline(unittest.TestCase):
    """fetch_area / load_area with the Overpass server replaced by a fake that
    returns the real OSM sample in Overpass JSON format (no internet needed)."""

    def test_fetch_and_cache(self):
        import tempfile
        import xml.etree.ElementTree as ET
        from unittest import mock

        import requests

        from aro.area import AreaSpec, load_area

        root = ET.parse(SAMPLE).getroot()
        elements = [{"type": "node", "id": int(n.get("id")), "lat": float(n.get("lat")), "lon": float(n.get("lon"))}
                    for n in root.iter("node")]
        for w in root.iter("way"):
            tags = {t.get("k"): t.get("v") for t in w.iter("tag")}
            elements.append({"type": "way", "id": int(w.get("id")), "tags": tags,
                             "nodes": [int(nd.get("ref")) for nd in w.iter("nd")]})
        elements.append({"type": "node", "id": 1, "lat": 60.53, "lon": 26.95,
                         "tags": {"amenity": "hospital", "name": "Test Hospital"}})
        calls = []

        class Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"elements": elements}

        def fake_post(url, data=None, **kw):
            calls.append(data["data"])
            return Resp()

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(requests, "post", fake_post):
            spec = AreaSpec(center=(60.53, 26.95), size_km=2, tile_km=1, name="mock", cache_dir=tmp)
            g, pois = load_area(spec, log=lambda *_: None)
            self.assertEqual(len(calls), 4)                       # 2 x 2 tiles downloaded
            self.assertIn('way["highway"~', calls[0])
            self.assertGreater(g.n, 100)
            self.assertEqual(sum(p["kind"] == "hospital" for p in pois), 1)   # de-duplicated across tiles
            g2, _ = load_area(spec, log=lambda *_: None)          # second time: graph cache
            self.assertEqual(len(calls), 4)
            self.assertEqual((g2.n, len(g2.edges)), (g.n, len(g.edges)))
            self.assertAlmostEqual(dijkstra(g2, 0, g2.n - 1).cost, dijkstra(g, 0, g.n - 1).cost, places=6)
