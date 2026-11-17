"""The real road network as a weighted directed graph.

    OpenStreetMap  --OSMnx-->  MultiDiGraph  -->  RoadNetwork (compact lists)

  vertex = road intersection (OSMnx-simplified graph)
  edge   = road between two intersections (one per direction it can be driven)
  weight = travel time in seconds
           = length / speed limit  x  traffic-profile factor  x  congestion factor
           (closed roads get weight = infinity)

The finished graph is pickled to data/cache, so OpenStreetMap is downloaded once.
"""

from __future__ import annotations

import gzip
import hashlib
import math
import pickle
import threading
import time
from pathlib import Path

from .geo import GridIndex, haversine, square_bbox

# Default speeds (km/h) for Indian city roads without a maxspeed tag.
INDIA_HWY_SPEEDS = {
    "motorway": 80, "motorway_link": 50, "trunk": 60, "trunk_link": 40,
    "primary": 50, "primary_link": 35, "secondary": 40, "secondary_link": 30,
    "tertiary": 35, "tertiary_link": 25, "unclassified": 30, "residential": 25,
    "living_street": 15, "service": 15, "road": 25,
}
MAJOR_ROADS = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
               "secondary", "secondary_link"}

# How much slower than the speed limit traffic moves, by road class.
TRAFFIC_PROFILES = {
    "free": {"label": "Free flow (night)", "major": 1.0, "minor": 1.0},
    "normal": {"label": "Normal traffic", "major": 1.4, "minor": 1.15},
    "peak": {"label": "Peak hour", "major": 2.2, "minor": 1.4},
}

CACHE_VERSION = "v3"


def _first(value, default=""):
    if isinstance(value, list):
        value = value[0] if value else default
    return default if value is None or (isinstance(value, float) and math.isnan(value)) else value


class RoadNetwork:
    def __init__(self, name: str = "road network"):
        self.name = name
        self.lat: list[float] = []
        self.lon: list[float] = []
        self.adj: list[list[tuple[int, int]]] = []
        self.e_u: list[int] = []
        self.e_v: list[int] = []
        self.e_len: list[float] = []      # metres
        self.e_time: list[float] = []     # free-flow seconds
        self.e_name: list[str] = []
        self.e_major: list[bool] = []
        self.e_geom: list[tuple] = []     # intermediate (lat, lon) points of each edge
        self.raw_osm_nodes = 0
        self.profile = "normal"
        self.blocked: set[int] = set()
        self.congestion: dict[int, float] = {}
        self.applied_signature = None     # which DB state (traffic + conditions) is applied
        self._init_runtime()

    def _init_runtime(self):
        self.lock = threading.RLock()
        self._weights = None
        self._node_index = None
        self._geo_index = None
        self._radj = None

    def __getstate__(self):
        d = self.__dict__.copy()
        for k in ("lock", "_weights", "_node_index", "_geo_index", "_radj"):
            d.pop(k, None)
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self._init_runtime()

    # ---------------------------------------------------------------- build
    @property
    def n(self) -> int:
        return len(self.lat)

    @property
    def m(self) -> int:
        return len(self.e_u)

    def add_node(self, lat: float, lon: float) -> int:
        self.lat.append(lat)
        self.lon.append(lon)
        self.adj.append([])
        return self.n - 1

    def add_edge(self, u: int, v: int, length_m: float, time_s: float, name: str = "", major: bool = False,
                 geom: tuple = ()) -> int:
        eid = self.m
        self.e_u.append(u)
        self.e_v.append(v)
        self.e_len.append(length_m)
        self.e_time.append(time_s)
        self.e_name.append(name)
        self.e_major.append(major)
        self.e_geom.append(geom)
        self.adj[u].append((v, eid))
        return eid

    @classmethod
    def from_osmnx(cls, G, name: str) -> "RoadNetwork":
        """Convert an OSMnx graph (with travel_time on every edge) into compact lists."""
        net = cls(name)
        idx = {}
        for node, data in G.nodes(data=True):
            idx[node] = net.add_node(float(data["y"]), float(data["x"]))
        for u, v, data in G.edges(data=True):
            ui, vi = idx[u], idx[v]
            hw = str(_first(data.get("highway"), "road"))
            nm = _first(data.get("name")) or _first(data.get("ref")) or hw.replace("_", " ")
            geom = ()
            g = data.get("geometry")
            if g is not None:
                pts = [(round(y, 6), round(x, 6)) for x, y in g.coords]
                # make sure the shape runs from u to v
                if haversine(*pts[0], net.lat[ui], net.lon[ui]) > haversine(*pts[-1], net.lat[ui], net.lon[ui]):
                    pts.reverse()
                geom = tuple(pts[1:-1])
            length = float(data.get("length", 0.0)) or haversine(net.lat[ui], net.lon[ui], net.lat[vi], net.lon[vi])
            ttime = float(data.get("travel_time", 0.0)) or length / (25 / 3.6)
            net.add_edge(ui, vi, length, ttime, str(nm), hw in MAJOR_ROADS, geom)
        return net

    # ------------------------------------------------------------- loading
    @staticmethod
    def build_osmnx_graph(cfg, log=print):
        """Download (or read the .osm file) and prepare the OSMnx graph."""
        import osmnx as ox

        ox.settings.use_cache = True
        ox.settings.cache_folder = str(Path(cfg.CACHE_DIR) / "osmnx_http")
        ox.settings.requests_timeout = 300
        ox.settings.max_query_area_size = 100 * 1000 * 1000  # split big areas into ~10x10 km Overpass queries
        ox.settings.log_console = False
        if cfg.ROAD_GRAPH_OSM_FILE:
            log(f"Reading road network from {cfg.ROAD_GRAPH_OSM_FILE} ...")
            G = ox.graph_from_xml(cfg.ROAD_GRAPH_OSM_FILE, simplify=True, retain_all=False)
        else:
            s, w, n, e = square_bbox(*cfg.AREA_CENTER, cfg.AREA_SIZE_KM)
            log(f"Downloading {cfg.AREA_SIZE_KM:g} x {cfg.AREA_SIZE_KM:g} km road network of {cfg.AREA_NAME} "
                f"from OpenStreetMap (OSMnx) ... first time only, this can take several minutes")
            G = ox.graph_from_bbox((w, s, e, n), network_type="drive", simplify=True)
        log(f"  OSMnx graph: {len(G):,} nodes, {G.number_of_edges():,} edges - adding speeds and travel times")
        G = ox.add_edge_speeds(G, hwy_speeds=INDIA_HWY_SPEEDS, fallback=25)
        G = ox.add_edge_travel_times(G)
        G = ox.truncate.largest_component(G, strongly=True)
        return G

    def export(self, path) -> int:
        """Write the finished graph as a compressed file to ship with a deployment."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wb", compresslevel=6) as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)
        return path.stat().st_size

    @classmethod
    def load_file(cls, path) -> "RoadNetwork":
        with gzip.open(path, "rb") as f:
            return pickle.load(f)

    @classmethod
    def load(cls, cfg, log=print) -> "RoadNetwork":
        if cfg.SERVERLESS:
            # no OSMnx on the server: use the graph built on your computer (manage.py export-graph)
            if not Path(cfg.ROAD_NETWORK_FILE).exists():
                raise RuntimeError(f"Road graph file not found ({Path(cfg.ROAD_NETWORK_FILE).name}). Run "
                                   "'python manage.py export-graph' locally and deploy backend/deploy/roadnet.pkl.gz")
            t0 = time.time()
            net = cls.load_file(cfg.ROAD_NETWORK_FILE)
            log(f"Road network loaded from {Path(cfg.ROAD_NETWORK_FILE).name} in {time.time() - t0:.1f}s")
            return net
        src = cfg.ROAD_GRAPH_OSM_FILE or f"{cfg.AREA_CENTER}|{cfg.AREA_SIZE_KM}"
        key = hashlib.sha1(f"{src}|{CACHE_VERSION}".encode()).hexdigest()[:14]
        cache = Path(cfg.CACHE_DIR) / f"roadnet_{key}.pkl"
        if cache.exists():
            log(f"Loading road network from cache ({cache.name}) ...")
            with open(cache, "rb") as f:
                return pickle.load(f)
        t0 = time.time()
        G = cls.build_osmnx_graph(cfg, log)
        net = cls.from_osmnx(G, cfg.AREA_NAME)
        net.raw_osm_nodes = len(G)
        del G
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "wb") as f:
            pickle.dump(net, f, protocol=pickle.HIGHEST_PROTOCOL)
        log(f"  road network ready: {net.n:,} intersections, {net.m:,} road edges ({time.time() - t0:.0f}s, cached)")
        return net

    # ------------------------------------------------------------- weights
    def weights(self) -> list[float]:
        """Current travel time of every edge (cached until conditions change)."""
        w = self._weights
        if w is None:
            p = TRAFFIC_PROFILES[self.profile]
            fmaj, fmin = p["major"], p["minor"]
            cong = self.congestion
            w = [t * (fmaj if mj else fmin) for t, mj in zip(self.e_time, self.e_major)]
            for eid, f in cong.items():
                w[eid] *= f
            for eid in self.blocked:
                w[eid] = math.inf
            self._weights = w
        return w

    @property
    def max_speed_mps(self) -> float:
        """Fastest possible speed on any road (traffic only slows down). Used for lower bounds."""
        if not hasattr(self, "_vmax"):
            self._vmax = max((l / t for l, t in zip(self.e_len, self.e_time) if t > 0), default=10.0)
        return self._vmax

    def set_profile(self, profile: str):
        if profile not in TRAFFIC_PROFILES:
            raise ValueError(f"unknown traffic profile {profile}")
        with self.lock:
            self.profile = profile
            self._weights = None

    def apply_conditions(self, conditions) -> dict:
        """Rebuild closures / congestion from the road_conditions table."""
        blocked, cong = set(), {}
        for c in conditions:
            if not c["active"]:
                continue
            eids = self.edges_near(c["lat"], c["lon"], c["radius_m"])
            if not eids and c["kind"] == "closure":
                eids = [self.nearest_edge(c["lat"], c["lon"])]
            eids = {t for e in eids for t in self.twins(e)}
            if c["kind"] == "closure":
                blocked |= eids
            else:
                for e in eids:
                    cong[e] = max(cong.get(e, 1.0), max(1.0, c["factor"]))
        with self.lock:
            self.blocked, self.congestion = blocked, cong
            self._weights = None
        return {"closed_edges": len(blocked), "congested_edges": len(cong)}

    # -------------------------------------------------------------- lookup
    def nearest_node(self, lat: float, lon: float) -> tuple[int, float]:
        if self._node_index is None:
            self._node_index = GridIndex(self.lat, self.lon)
        return self._node_index.nearest(lat, lon)

    def _is_open(self, node: int, w) -> bool:
        """Can traffic both enter and leave this node on open roads?"""
        if self._radj is None:
            radj = [[] for _ in range(self.n)]
            for eid, v in enumerate(self.e_v):
                radj[v].append(eid)
            self._radj = radj
        return any(w[e] != math.inf for _, e in self.adj[node]) and any(w[e] != math.inf for e in self._radj[node])

    def nearest_open_node(self, lat: float, lon: float, search_m: float = 600) -> tuple[int, float]:
        """Nearest intersection that is not cut off by a road closure."""
        node, d = self.nearest_node(lat, lon)
        w = self.weights()
        if self._is_open(node, w):
            return node, d
        near = sorted((haversine(lat, lon, self.lat[i], self.lon[i]), i)
                      for i in self._node_index.within(lat, lon, search_m))
        for dist, i in near:
            if self._is_open(i, w):
                return i, dist
        return node, d

    def _geometry(self):
        if self._geo_index is None:
            lats, lons, owner = [], [], []
            for eid in range(self.m):
                pts = [(self.lat[self.e_u[eid]], self.lon[self.e_u[eid]]), *self.e_geom[eid],
                       (self.lat[self.e_v[eid]], self.lon[self.e_v[eid]])]
                for (a1, b1), (a2, b2) in zip(pts, pts[1:]):
                    lats.append((a1 + a2) / 2)
                    lons.append((b1 + b2) / 2)
                    owner.append(eid)
                for a, b in pts[1:-1]:
                    lats.append(a)
                    lons.append(b)
                    owner.append(eid)
            self._geo_index = (GridIndex(lats, lons), owner)
        return self._geo_index

    def edges_near(self, lat: float, lon: float, radius_m: float) -> list[int]:
        idx, owner = self._geometry()
        return sorted({owner[i] for i in idx.within(lat, lon, radius_m)})

    def nearest_edge(self, lat: float, lon: float) -> int:
        idx, owner = self._geometry()
        return owner[idx.nearest(lat, lon)[0]]

    def twins(self, eid: int) -> list[int]:
        """The edge and the opposite direction of the same road."""
        u, v = self.e_u[eid], self.e_v[eid]
        out = [eid]
        for w, k in self.adj[v]:
            if w == u and k != eid and abs(self.e_len[k] - self.e_len[eid]) < 2:
                out.append(k)
        return out

    def edge_shape(self, eid: int) -> list[list[float]]:
        u, v = self.e_u[eid], self.e_v[eid]
        return [[self.lat[u], self.lon[u]], *[list(p) for p in self.e_geom[eid]], [self.lat[v], self.lon[v]]]

    def route_coords(self, edges: list[int], start_node: int | None = None) -> list[list[float]]:
        if not edges:
            return [[self.lat[start_node], self.lon[start_node]]] if start_node is not None else []
        out = self.edge_shape(edges[0])
        for eid in edges[1:]:
            out += self.edge_shape(eid)[1:]
        return [[round(a, 6), round(b, 6)] for a, b in out]

    def route_summary(self, edges: list[int]) -> dict:
        w = self.weights()
        roads = []
        for eid in edges:
            nm = self.e_name[eid]
            if not roads or roads[-1] != nm:
                roads.append(nm)
        return {"distance_m": sum(self.e_len[e] for e in edges), "time_s": sum(w[e] for e in edges), "roads": roads}

    def bounds(self):
        return (min(self.lat), min(self.lon), max(self.lat), max(self.lon))

    def summary(self) -> dict:
        s, w, n, e = self.bounds()
        return {"name": self.name, "intersections": self.n, "road_edges": self.m,
                "road_km": round(sum(self.e_len) / 2000, 0),  # two directions per road
                "bounds": [[s, w], [n, e]],
                "size_km": [round(haversine(s, w, n, w) / 1000, 1), round(haversine(s, w, s, e) / 1000, 1)],
                "traffic_profile": self.profile}

    def shapes_in_view(self, s, w, n, e, limit=50_000) -> list:
        seen, out = set(), []
        for eid in range(self.m):
            u, v = self.e_u[eid], self.e_v[eid]
            if not ((s <= self.lat[u] <= n and w <= self.lon[u] <= e) or (s <= self.lat[v] <= n and w <= self.lon[v] <= e)):
                continue
            key = (min(u, v), max(u, v), round(self.e_len[eid]))
            if key in seen:
                continue
            seen.add(key)
            out.append([[round(a, 5), round(b, 5)] for a, b in self.edge_shape(eid)])
            if len(out) >= limit:
                break
        return out
