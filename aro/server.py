"""Flask application factory and start-up.

Start-up (the same locally and on Vercel), done on the first request that needs it:
  1. connect to the database, create tables, emergency types
  2. road data: from the osm_cache table, or downloaded once from OpenStreetMap
  3. build the road graph (pure Python, ~1-2 s for 5 x 5 km)
  4. first run only: store the OSM hospitals and create the simulated ambulances
Any problem is shown on /api/status (and retried), never as a crash.
"""

from __future__ import annotations

import gc
import gzip
import json
import threading
import time
import traceback

from flask import Flask, jsonify, send_from_directory
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .api import bp
from .config import Config
from .db import init_engine, session_scope
from .geo import square_bbox
from .models import OsmCache, RoadCondition, Setting
from .osm import download, load_osm_xml
from .road_network import RoadNetwork
from .seed import seed_ambulances, seed_emergency_types, seed_hospitals

RETRY_SECONDS = 15
CACHE_FORMAT = "v1"


class Runtime:
    def __init__(self, cfg=Config):
        self.cfg = cfg
        self.status = "starting"   # starting | loading | ready | error
        self.log: list[str] = []
        self.error: str | None = None
        self.net: RoadNetwork | None = None
        self.db_ok = False
        self._lock = threading.Lock()
        self._last_try = 0.0

    def say(self, msg: str):
        print(msg, flush=True)
        self.log.append(msg)

    def ensure_db(self):
        """Quick: only the database (used by endpoints that do not need the road graph)."""
        if self.db_ok:
            return
        try:
            init_database(self, self.cfg)
        except Exception as e:
            traceback.print_exc()
            self.error = explain(self.cfg, e)

    def ensure_loaded(self, wait: bool = True):
        """Load everything (idempotent). wait=False: return at once if another thread is loading."""
        if self.net is not None:
            return
        if not self._lock.acquire(blocking=wait):
            return
        try:
            self._load()
        finally:
            self._lock.release()

    def _load(self):
        if self.net is not None:
            return
        if self.status == "error" and time.time() - self._last_try < RETRY_SECONDS:
            return   # don't hammer OpenStreetMap / the database: retry every RETRY_SECONDS
        self._last_try = time.time()
        self.status, self.error, self.log = "loading", None, []
        try:
            if not self.db_ok:
                init_database(self, self.cfg)
            net = prepare_network(self, self.cfg)
            sync_network(net)
            net.weights()
            gc.collect()
            gc.freeze()   # many long-lived objects: keep the garbage collector from rescanning them
            self.net = net
            self.status = "ready"
            self.say(f"Ready: {net.n:,} intersections, {net.m:,} road edges.")
        except Exception as e:
            traceback.print_exc()
            self.status = "error"
            self.error = explain(self.cfg, e)
            self.say(f"ERROR: {self.error}")


def explain(cfg, e: Exception) -> str:
    text = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
    low = text.lower()
    if "password authentication" in low or "could not translate host" in low or "connection" in low and "failed" in low:
        host = cfg.DATABASE_URL.split("@")[-1].split("/")[0]
        return f"Cannot connect to the database ({host}). Check DATABASE_URL. Details: {text}"
    return text


def init_database(runtime: Runtime, cfg):
    init_engine(cfg.DATABASE_URL, serverless=cfg.SERVERLESS)
    try:
        with session_scope() as s:
            seed_emergency_types(s)
    except IntegrityError:   # another server instance did it at the same moment
        pass
    runtime.db_ok = True
    if cfg.DB_MODE == "sqlite-temporary":
        runtime.say("Demo mode: no DATABASE_URL set, using a temporary database (data resets on restarts).")


def _cache_key(cfg) -> str:
    c = cfg.AREA_CENTER
    return f"osm|{c[0]:.5f},{c[1]:.5f}|{cfg.AREA_SIZE_KM:g}km|{CACHE_FORMAT}"


def get_osm_data(runtime: Runtime, cfg) -> dict:
    """OSM roads + hospitals: database cache first, otherwise download once and store."""
    if cfg.ROAD_GRAPH_OSM_FILE:
        runtime.say(f"Reading roads from {cfg.ROAD_GRAPH_OSM_FILE} ...")
        return load_osm_xml(cfg.ROAD_GRAPH_OSM_FILE)
    key = _cache_key(cfg)
    with session_scope() as s:
        row = s.get(OsmCache, key)
        if row is not None:
            runtime.say("Loading road map from the database ...")
            return json.loads(gzip.decompress(row.data))
    runtime.say(f"First start: downloading the {cfg.AREA_SIZE_KM:g} x {cfg.AREA_SIZE_KM:g} km road map of "
                f"{cfg.AREA_NAME} from OpenStreetMap (about 10-40 s) ...")
    osm = download(square_bbox(*cfg.AREA_CENTER, cfg.AREA_SIZE_KM), log=runtime.say)
    blob = gzip.compress(json.dumps(osm, separators=(",", ":")).encode(), 6)
    try:
        with session_scope() as s:
            s.add(OsmCache(key=key, data=blob))
        runtime.say(f"  saved in the database ({len(blob) / 1e6:.1f} MB) - next starts are fast")
    except IntegrityError:
        pass  # stored by another instance meanwhile
    return osm


def prepare_network(runtime: Runtime, cfg, osm: dict | None = None) -> RoadNetwork:
    osm = osm if osm is not None else get_osm_data(runtime, cfg)
    t0 = time.time()
    net = RoadNetwork.from_osm(osm, cfg.AREA_NAME)
    runtime.say(f"Road graph built: {net.n:,} intersections, {net.m:,} edges ({time.time() - t0:.1f}s)")
    for step in (lambda s: seed_hospitals(s, cfg, osm.get("pois", []), log=runtime.say),
                 lambda s: seed_ambulances(s, net, cfg, log=runtime.say)):
        try:
            with session_scope() as s:
                step(s)
        except IntegrityError:   # seeded by another instance at the same moment
            pass
    return net


def sync_network(net: RoadNetwork):
    """Apply traffic level + road conditions from the database if they changed.
    Several server instances can run at once on Vercel; the database is the source of truth."""
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


def create_app(cfg=Config, osm: dict | None = None, preload: bool | None = None) -> Flask:
    """osm can be injected (tests). preload: load in a background thread at start (default: locally yes)."""
    app = Flask(__name__, static_folder=None)
    app.config["ARO"] = cfg
    app.json.sort_keys = False
    runtime = Runtime(cfg)
    app.extensions["aro_runtime"] = runtime
    app.register_blueprint(bp)

    if osm is not None:          # tests: build synchronously from the given data
        init_database(runtime, cfg)
        net = prepare_network(runtime, cfg, osm)
        sync_network(net)
        runtime.net, runtime.status = net, "ready"
    elif preload if preload is not None else not cfg.SERVERLESS:
        threading.Thread(target=runtime.ensure_loaded, daemon=True).start()

    public = cfg.PUBLIC_DIR

    @app.get("/")
    @app.get("/<path:path>")
    def frontend(path="index.html"):
        """The React app (on Vercel these files are served by the CDN from public/)."""
        if path.startswith("api/"):
            return jsonify({"error": "not found"}), 404
        if (public / path).is_file():
            return send_from_directory(public, path)
        if (public / "index.html").is_file():
            return send_from_directory(public, "index.html")
        return ("<h2>Ambulance Route Optimizer</h2><p>The web app has not been built yet: run "
                "<code>npm install</code> and <code>npm run build</code> in <code>frontend/</code>. "
                "API status: <a href='/api/status'>/api/status</a></p>")

    return app
