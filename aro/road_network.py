"""The real road network as a weighted directed graph.

    OpenStreetMap (Overpass)  -->  raw segments  -->  simplified graph of intersections

  vertex = road intersection (OSMnx-simplified graph)
  edge   = road between two intersections (one per direction it can be driven)
  weight = travel time in seconds
           = length / speed limit  x  traffic-profile factor  x  congestion factor
           (closed roads get weight = infinity)

The downloaded OpenStreetMap data is stored in the database (osm_cache table),
so it is downloaded only once - even on Vercel, where files do not persist.
"""

from __future__ import annotations

import math
import re
import threading

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



def parse_maxspeed(value) -> float | None:
    """'50', '30 mph', 'IN:urban' -> km/h (None if unusable)."""
    if not value:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(value))
    if not m:
        return None
    v = float(m.group(1)) * (1.609344 if "mph" in str(value) else 1)
    return v if 5 <= v <= 150 else None


def oneway_direction(tags: dict) -> int:
    """+1 forward only, -1 backward only, 0 both directions."""
    ow = (tags.get("oneway") or "").lower()
    if ow in ("yes", "true", "1"):
        return 1
    if ow in ("-1", "reverse"):
        return -1
    if ow == "no":
        return 0
    if tags.get("highway") in ("motorway", "motorway_link") or tags.get("junction") in ("roundabout", "circular"):
        return 1
    return 0


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
    def from_osm(cls, osm: dict, name: str) -> "RoadNetwork":
        """OSM data (see osm.py) -> graph of intersections.

        1. every pair of consecutive points on a road = one segment (two if the road is two-way)
        2. simplification: chains of points that are not intersections (exactly two
           neighbours, traffic just passes through) become ONE edge that keeps the
           road's shape - like OSMnx's simplify_graph(). Distances do not change.
        3. keep the largest strongly connected component (Kosaraju), so every
           intersection can reach every other one."""
        nodes = {int(k): v for k, v in osm["nodes"].items()}
        idx: dict[int, int] = {}
        lat, lon = [], []
        eu, ev, elen, etime, eway = [], [], [], [], []
        way_name, way_major = [], []
        for wi, (_wid, tags, refs) in enumerate(osm["ways"]):
            hw = tags.get("highway", "road")
            way_name.append(tags.get("name:en") or tags.get("name") or tags.get("ref") or hw.replace("_", " "))
            way_major.append(hw in MAJOR_ROADS)
            refs = [r for r in refs if r in nodes]
            if len(refs) < 2:
                continue
            speed = (parse_maxspeed(tags.get("maxspeed")) or INDIA_HWY_SPEEDS.get(hw, 25)) / 3.6
            direction = oneway_direction(tags)
            ids = []
            for r in refs:
                i = idx.get(r)
                if i is None:
                    i = idx[r] = len(lat)
                    lat.append(nodes[r][0])
                    lon.append(nodes[r][1])
                ids.append(i)
            for u, v in zip(ids, ids[1:]):
                if u == v:
                    continue
                length = haversine(lat[u], lon[u], lat[v], lon[v])
                if direction >= 0:
                    eu.append(u); ev.append(v); elen.append(length); etime.append(length / speed); eway.append(wi)
                if direction <= 0:
                    eu.append(v); ev.append(u); elen.append(length); etime.append(length / speed); eway.append(wi)
        n, m = len(lat), len(eu)
        if not m:
            raise RuntimeError("No drivable roads in this area")
        out_e = [[] for _ in range(n)]
        in_e = [[] for _ in range(n)]
        for i in range(m):
            out_e[eu[i]].append(i)
            in_e[ev[i]].append(i)

        def interior(x: int) -> bool:
            oe, ie = out_e[x], in_e[x]
            if len(oe) > 2 or len(ie) > 2:
                return False
            outs = {ev[i] for i in oe}
            ins = {eu[i] for i in ie}
            nb = outs | ins
            if len(nb) != 2 or x in nb:
                return False
            if len(oe) == 2 and len(ie) == 2 and outs == ins:      # two-way road
                return True
            return len(oe) == 1 and len(ie) == 1 and outs != ins   # one-way road

        is_int = [interior(x) for x in range(n)]
        g = cls(name)
        new_id = {}
        for x in range(n):
            if not is_int[x]:
                new_id[x] = g.add_node(lat[x], lon[x])
        for u in new_id:
            for e0 in out_e[u]:
                length, tsec, geom = elen[e0], etime[e0], []
                prev, cur, ok, steps = u, ev[e0], True, 0
                while is_int[cur]:
                    steps += 1
                    if steps > n:
                        ok = False
                        break
                    geom.append((round(lat[cur], 6), round(lon[cur], 6)))
                    nxt = [k for k in out_e[cur] if ev[k] != prev]
                    if not nxt:
                        ok = False
                        break
                    k = nxt[0]
                    length += elen[k]
                    tsec += etime[k]
                    prev, cur = cur, ev[k]
                if ok:
                    wi = eway[e0]
                    g.add_edge(new_id[u], new_id[cur], length, tsec, way_name[wi], way_major[wi], tuple(geom))
        g = g.largest_component()
        g.raw_osm_nodes = n
        return g

    def strongly_connected_components(self) -> list[list[int]]:
        """Kosaraju's algorithm, iterative. O(V + E)."""
        n = self.n
        radj = [[] for _ in range(n)]
        for eid in range(self.m):
            radj[self.e_v[eid]].append(self.e_u[eid])
        seen = [False] * n
        order = []
        for s0 in range(n):
            if seen[s0]:
                continue
            seen[s0] = True
            stack = [(s0, 0)]
            while stack:
                u, i = stack[-1]
                if i < len(self.adj[u]):
                    stack[-1] = (u, i + 1)
                    v = self.adj[u][i][0]
                    if not seen[v]:
                        seen[v] = True
                        stack.append((v, 0))
                else:
                    stack.pop()
                    order.append(u)
        comp = [-1] * n
        comps = []
        for s0 in reversed(order):
            if comp[s0] != -1:
                continue
            comp[s0] = len(comps)
            members, stack = [s0], [s0]
            while stack:
                u = stack.pop()
                for v in radj[u]:
                    if comp[v] == -1:
                        comp[v] = comp[s0]
                        members.append(v)
                        stack.append(v)
            comps.append(members)
        return comps

    def largest_component(self) -> "RoadNetwork":
        keep = max(self.strongly_connected_components(), key=len)
        new = {old: i for i, old in enumerate(keep)}
        g = RoadNetwork(self.name)
        for old in keep:
            g.add_node(self.lat[old], self.lon[old])
        for eid in range(self.m):
            a, b = new.get(self.e_u[eid]), new.get(self.e_v[eid])
            if a is not None and b is not None:
                g.add_edge(a, b, self.e_len[eid], self.e_time[eid], self.e_name[eid], self.e_major[eid], self.e_geom[eid])
        return g

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
