"""Flask application factory.

Local server : the road network is loaded (or downloaded) in a background thread at start-up.
Vercel       : (SERVERLESS) the pre-built graph file is loaded on the first request that needs it.
"""

from __future__ import annotations

import gc
import os
import threading
import time
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
        self.db_ok = False
        self._load_lock = threading.Lock()
        self._last_try = 0.0

    def say(self, msg: str):
        print(msg, flush=True)
        self.log.append(msg)

    def ensure_loaded(self):
        """Serverless: connect to the database and load the graph on first use.
        If something is missing (database not connected, not seeded, no graph file),
        the error is shown on the status page and retried every 15 s - so fixing the
        database (e.g. running manage.py seed) works without redeploying."""
        if self.net is not None:
            return
        with self._load_lock:
            if self.net is not None or time.time() - self._last_try < 15:
                return
            self._last_try = time.time()
            self.status, self.error, self.log = "loading", None, []
            if not self.db_ok and not init_database(self, self.cfg):
                self.status = "error"
                return
            prepare(self, self.cfg)


def explain_db_error(cfg, e: Exception) -> str:
    if cfg.SERVERLESS and not (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")):
        return ("DATABASE_URL is not set. In Vercel open the backend project -> Storage -> connect a Neon "
                "database, then Deployments -> Redeploy (new environment variables only reach new deployments).")
    first = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
    host = cfg.DATABASE_URL.split("@")[-1].split("/")[0]
    return f"Cannot connect to the database at {host}: {first}"


def init_database(runtime: Runtime, cfg) -> bool:
    """Create the tables and emergency types. Returns False (with a readable error) on failure."""
    try:
        init_engine(cfg.DATABASE_URL, serverless=cfg.SERVERLESS)
        with session_scope() as s:
            seed_emergency_types(s)
        runtime.db_ok = True
        return True
    except Exception as e:
        traceback.print_exc()
        runtime.error = explain_db_error(cfg, e)
        runtime.say(f"ERROR: {runtime.error}")
        return False


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
    runtime = Runtime(cfg)
    if cfg.SERVERLESS:
        # never crash on import (that gives Vercel's blank "500 FUNCTION_INVOCATION_FAILED");
        # problems are reported by /api/status instead
        init_database(runtime, cfg)
    else:
        init_engine(cfg.DATABASE_URL, serverless=False)
        with session_scope() as s:
            seed_emergency_types(s)
        runtime.db_ok = True
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
            if cfg.SERVERLESS:
                runtime.ensure_loaded()
            msg = (f"<p style='color:#c8102e'><b>Problem:</b> {runtime.error}</p>" if runtime.error else
                   f"<p>Status: <b>{runtime.status}</b></p>")
            return ("<h2>Ambulance Route Optimizer - backend</h2>" + msg +
                    "<p>API status: <a href='/api/status'>/api/status</a>. Open the app through the frontend "
                    "(Vercel frontend project, or <code>npm run dev</code> locally).</p>")
        target = dist / path
        if path != "index.html" and target.is_file():
            return send_from_directory(dist, path)
        return send_from_directory(dist, "index.html")

    return app
