"""Flask application factory.

Local server : the road network is loaded (or downloaded) in a background thread at start-up.
Vercel       : (SERVERLESS) the pre-built graph file is loaded on the first request that needs it.
"""

from __future__ import annotations

import gc
import threading
import traceback

from flask import Flask, jsonify, send_from_directory
from sqlalchemy import func, select

from .api import bp
from .config import Config
from .db import init_engine, session_scope
from .models import Ambulance, Hospital, RoadCondition, Setting
from .road_network import RoadNetwork
from .seed import seed_ambulances, seed_emergency_types, seed_hospitals


class Runtime:
    def __init__(self, cfg=Config):
        self.cfg = cfg
        self.status = "starting"   # starting | loading | ready | error
        self.log: list[str] = []
        self.error: str | None = None
        self.net: RoadNetwork | None = None
        self._load_lock = threading.Lock()

    def say(self, msg: str):
        print(msg, flush=True)
        self.log.append(msg)

    def ensure_loaded(self):
        """Serverless: load the graph on first use (each cold start)."""
        if self.net is not None or self.status == "error":
            return
        with self._load_lock:
            if self.net is None and self.status != "error":
                prepare(self, self.cfg)


def sync_network(net: RoadNetwork):
    """Apply the traffic level + road conditions stored in PostgreSQL, if they changed.
    Several server instances can run at once (Vercel), so the database is the source of truth."""
    with session_scope() as s:
        row = s.get(Setting, "traffic_profile")
        profile = row.value if row else "normal"
        conds = [c.to_dict() for c in s.scalars(select(RoadCondition).where(RoadCondition.active.is_(True))
                                                .order_by(RoadCondition.id))]
    sig = (profile, tuple((c["id"], c["kind"], c["lat"], c["lon"], c["radius_m"], c["factor"]) for c in conds))
    if getattr(net, "applied_signature", None) != sig:
        if net.profile != profile:
            net.set_profile(profile)
        net.apply_conditions(conds)
        net.applied_signature = sig


def prepare(runtime: Runtime, cfg, net: RoadNetwork | None = None, hospitals: list[dict] | None = None):
    """Load the road graph, seed hospitals/ambulances, apply road conditions."""
    try:
        runtime.status = "loading"
        if net is None:
            net = RoadNetwork.load(cfg, log=runtime.say)
        if cfg.SERVERLESS:
            # seeding needs OSMnx, so on Vercel it must have been done with manage.py beforehand
            with session_scope() as s:
                if not s.scalar(select(func.count(Hospital.id))) or not s.scalar(select(func.count(Ambulance.id))):
                    raise RuntimeError("The database has no hospitals/ambulances yet. On your computer, set "
                                       "DATABASE_URL to this deployment's database and run: python manage.py seed")
        else:
            with session_scope() as s:
                seed_hospitals(s, cfg, log=runtime.say, hospitals=hospitals)
            with session_scope() as s:
                seed_ambulances(s, net, cfg, log=runtime.say)
        sync_network(net)
        net.weights()
        # millions of long-lived objects: keep the garbage collector from rescanning them
        gc.collect()
        gc.freeze()
        runtime.net = net
        runtime.status = "ready"
        runtime.say(f"Ready: {net.n:,} intersections, {net.m:,} road edges.")
    except Exception as e:
        traceback.print_exc()
        runtime.status = "error"
        runtime.error = str(e)
        runtime.say(f"ERROR: {e}")


def create_app(cfg=Config, net: RoadNetwork | None = None, hospitals: list[dict] | None = None,
               background: bool = True) -> Flask:
    """net / hospitals can be injected (tests, offline demo)."""
    app = Flask(__name__, static_folder=None)
    app.config["ARO"] = cfg
    app.json.sort_keys = False
    init_engine(cfg.DATABASE_URL, serverless=cfg.SERVERLESS)
    with session_scope() as s:
        seed_emergency_types(s)

    runtime = Runtime(cfg)
    app.extensions["aro_runtime"] = runtime
    app.register_blueprint(bp)

    if cfg.SERVERLESS and net is None:
        pass  # loaded lazily by Runtime.ensure_loaded() on the first request that needs it
    elif background:
        threading.Thread(target=prepare, args=(runtime, cfg, net, hospitals), daemon=True).start()
    else:
        prepare(runtime, cfg, net, hospitals)

    dist = cfg.FRONTEND_DIST

    @app.get("/")
    @app.get("/<path:path>")
    def frontend(path="index.html"):
        if path.startswith("api/"):
            return jsonify({"error": "not found"}), 404
        if not (dist / "index.html").exists():
            return ("<h2>Backend is running.</h2><p>Start the React app with <code>npm run dev</code> in "
                    "<code>frontend/</code> and open http://localhost:5173 - or run <code>npm run build</code> "
                    "and reload this page.</p>")
        target = dist / path
        if path != "index.html" and target.is_file():
            return send_from_directory(dist, path)
        return send_from_directory(dist, "index.html")

    return app
