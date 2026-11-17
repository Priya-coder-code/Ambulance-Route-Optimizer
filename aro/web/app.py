"""Flask web app: real OpenStreetMap map, Dijkstra and A* side by side."""

from __future__ import annotations

import math
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from ..algorithms import astar, dijkstra
from ..area import AreaSpec, load_area, make_dispatcher

HERE = Path(__file__).resolve().parent
MAX_EXPLORED_POINTS = 40_000   # explored dots sent to the browser (evenly sampled)
MAX_ROAD_SEGMENTS = 60_000     # road-graph overlay limit per request


class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        self.graph = None
        self.dispatcher = None
        self.spec = AreaSpec()
        self.loading = False
        self.load_log: list[str] = []
        self.load_error: str | None = None

    def install(self, graph, pois):
        disp = make_dispatcher(graph, pois)
        with self.lock:
            self.graph, self.dispatcher = graph, disp

    def load_async(self, spec: AreaSpec):
        self.loading, self.load_error, self.load_log = True, None, []

        def work():
            try:
                g, pois = load_area(spec, log=self.load_log.append)
                self.install(g, pois)
                self.spec = spec
            except Exception as e:  # network down, place not found ...
                self.load_error = str(e)
            finally:
                self.loading = False

        threading.Thread(target=work, daemon=True).start()


STATE = AppState()


# ----------------------------------------------------------------- helpers
def _sample(seq, k):
    if len(seq) <= k:
        return seq
    step = len(seq) / k
    return [seq[int(i * step)] for i in range(k)]


def result_json(res, graph) -> dict:
    stats = graph.path_stats(res.path_edges) if res.found else {"distance_m": None, "time_s": None, "roads": []}
    explored = _sample(res.explored_order, MAX_EXPLORED_POINTS)
    lat, lon = graph.lat, graph.lon
    return {
        "algorithm": res.algorithm,
        "found": res.found,
        "cost": None if math.isinf(res.cost) else res.cost,
        "nodes_explored": res.nodes_explored,
        "edges_relaxed": res.edges_relaxed,
        "heap_pushes": res.heap_pushes,
        "max_frontier": res.max_frontier,
        "runtime_ms": round(res.runtime_ms, 3),
        "path": graph.path_coords(res.path_edges, res.source) if res.found else [],
        "path_nodes": len(res.path),
        "distance_m": stats["distance_m"],
        "time_s": stats["time_s"],
        "roads": stats["roads"][:14],
        "explored": [[round(lat[i], 5), round(lon[i], 5)] for i in explored],
    }


def edge_shapes(graph, eids, limit=None) -> list:
    """Polyline for each road (one per two-way pair)."""
    seen, out = set(), []
    for eid in eids:
        e = graph.edges[eid]
        key = (min(e.u, e.v), max(e.u, e.v), round(e.length_m))
        if key in seen:
            continue
        seen.add(key)
        out.append([[round(a, 5), round(b, 5)] for a, b in graph.edge_coords(eid)])
        if limit and len(out) >= limit:
            break
    return out


def state_json() -> dict:
    g, d = STATE.graph, STATE.dispatcher
    s, w, n, e = g.bounds()
    return {
        "summary": g.summary(),
        "bounds": [[s, w], [n, e]],
        "hospitals": [h.to_dict() for h in d.hospitals],
        "ambulances": [a.to_dict() for a in d.ambulances],
        "blocked": edge_shapes(g, sorted(g.blocked)),
        "jams": edge_shapes(g, sorted(g.traffic)),
    }


def _err(msg, code=400):
    return jsonify({"error": msg}), code


def _same(a: float, b: float) -> bool:
    if math.isinf(a) or math.isinf(b):
        return math.isinf(a) and math.isinf(b)
    return abs(a - b) <= 1e-6 * max(1.0, abs(a))


# ---------------------------------------------------------------- the app
def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(HERE / "templates"), static_folder=str(HERE / "static"))

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def api_state():
        if STATE.graph is None:
            return _err("no map loaded", 503)
        return jsonify(state_json())

    @app.get("/api/roads")
    def api_roads():
        """Road-graph edges inside the visible map area (drawn as 'the graph')."""
        g = STATE.graph
        s, w, n, e = (float(request.args[k]) for k in ("s", "w", "n", "e"))
        lat, lon = g.lat, g.lon
        eids = (i for i, ed in enumerate(g.edges)
                if (s <= lat[ed.u] <= n and w <= lon[ed.u] <= e) or (s <= lat[ed.v] <= n and w <= lon[ed.v] <= e))
        shapes = edge_shapes(g, eids, limit=MAX_ROAD_SEGMENTS)
        return jsonify({"roads": shapes, "truncated": len(shapes) >= MAX_ROAD_SEGMENTS})

    @app.post("/api/load")
    def api_load():
        if STATE.loading:
            return _err("Already loading an area")
        body = request.get_json(force=True)
        place = (body.get("place") or "").strip()
        size = float(body.get("size_km") or 10)
        if not 1 <= size <= 60:
            return _err("Area size must be between 1 and 60 km")
        if not place:
            return _err("Type a place name, e.g. 'Connaught Place, New Delhi', or lat,lon")
        try:
            a, b = (float(x) for x in place.split(","))
            spec = AreaSpec(center=(a, b), size_km=size, name=f"{size:g} x {size:g} km around {a:.4f}, {b:.4f}")
        except ValueError:
            spec = AreaSpec(center=None, place=place, size_km=size, name=None)
        STATE.load_async(spec)
        return jsonify({"ok": True})

    @app.get("/api/load_status")
    def api_load_status():
        out = {"loading": STATE.loading, "log": STATE.load_log[-8:], "error": STATE.load_error}
        if not STATE.loading and not STATE.load_error and STATE.graph is not None:
            out["state"] = state_json()
        return jsonify(out)

    @app.post("/api/route")
    def api_route():
        body = request.get_json(force=True)
        metric = body.get("metric", "time")
        with STATE.lock:
            g = STATE.graph
            s, _ = g.nearest_node(*body["from"])
            t, _ = g.nearest_node(*body["to"])
            rd = dijkstra(g, s, t, metric)
            ra = astar(g, s, t, metric)
            return jsonify({
                "metric": metric,
                "from": [g.lat[s], g.lon[s]], "to": [g.lat[t], g.lon[t]],
                "results": {"dijkstra": result_json(rd, g), "astar": result_json(ra, g)},
                "same_cost": _same(rd.cost, ra.cost),
            })

    @app.post("/api/dispatch")
    def api_dispatch():
        body = request.get_json(force=True)
        metric = body.get("metric", "time")
        lat, lon = body["incident"]
        with STATE.lock:
            g, d = STATE.graph, STATE.dispatcher
            try:
                out = d.dispatch(lat, lon, metric=metric, mark_busy=bool(body.get("mark_busy")))
            except RuntimeError as e:
                return _err(str(e))
            legs = {}
            for algo, leg in out["legs"].items():
                (r1, amb), (r2, hosp) = leg["to_patient"], leg["to_hospital"]
                legs[algo] = {
                    "to_patient": result_json(r1, g), "ambulance": amb.to_dict() if amb else None,
                    "to_hospital": result_json(r2, g), "hospital": hosp.to_dict() if hosp else None,
                }
            inc = out["incident"]
            same = all(_same(out["legs"]["dijkstra"][k][0].cost, out["legs"]["astar"][k][0].cost)
                       for k in ("to_patient", "to_hospital"))
            return jsonify({
                "metric": metric,
                "incident": [g.lat[inc["node"]], g.lon[inc["node"]]],
                "legs": legs, "same_cost": same,
                "ambulances": [a.to_dict() for a in d.ambulances],
            })

    @app.post("/api/traffic")
    def api_traffic():
        body = request.get_json(force=True)
        with STATE.lock:
            g = STATE.graph
            action = body.get("action")
            if action == "block":
                g.block_near(body["lat"], body["lon"], float(body.get("radius_m", 30)))
            elif action == "jam":
                g.jam_near(body["lat"], body["lon"], float(body.get("radius_m", 400)), float(body.get("factor", 4)))
            elif action == "clear":
                g.clear_traffic()
            else:
                return _err("unknown action")
            return jsonify(state_json())

    @app.post("/api/place")
    def api_place():
        body = request.get_json(force=True)
        with STATE.lock:
            d = STATE.dispatcher
            kind = body.get("kind")
            if kind == "hospital":
                d.add_hospital(body.get("name") or f"Hospital {len(d.hospitals) + 1}", body["lat"], body["lon"])
            elif kind == "ambulance":
                d.add_ambulance(body.get("name") or f"Ambulance {len(d.ambulances) + 1}", body["lat"], body["lon"])
            elif kind == "reset_ambulances":
                d.reset_ambulances()
            else:
                return _err("unknown kind")
            return jsonify(state_json())

    return app


def serve(graph, pois, host="127.0.0.1", port=5000):
    STATE.install(graph, pois)
    app = create_app()
    print(f"\n  Ambulance Route Optimizer running at  http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
