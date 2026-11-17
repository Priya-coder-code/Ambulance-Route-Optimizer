"""Directed, weighted road graph built from OpenStreetMap data.

  * vertices = road intersections (after simplification)
  * edges    = road stretches between two intersections; each keeps its
               real shape (list of OSM points) for drawing on the map
  * weight   = travel time in seconds  = length / speed * traffic_factor
               (or plain length in metres when metric == "distance")

One-way streets become single directed edges. Only the largest strongly
connected component is kept, so every node can reach every other node.
"""

from __future__ import annotations

import math
import pickle
import random
import re
from pathlib import Path

from .geo import GridIndex, haversine

# Typical Indian urban speeds (km/h) used when a road has no maxspeed tag.
DEFAULT_SPEED_KMH = {
    "motorway": 80, "motorway_link": 50,
    "trunk": 60, "trunk_link": 40,
    "primary": 50, "primary_link": 35,
    "secondary": 40, "secondary_link": 30,
    "tertiary": 35, "tertiary_link": 25,
    "unclassified": 30, "residential": 25, "road": 25,
    "living_street": 15, "service": 15,
}

METRICS = ("time", "distance")
PROJECTION_SAFETY = 0.99  # shrink the projected straight-line distance by 1 % (keeps A* admissible)


def parse_maxspeed(value: str | None) -> float | None:
    if not value:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", value)
    if not m:
        return None
    v = float(m.group(1))
    if "mph" in value:
        v *= 1.609344
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


class Edge:
    """A directed road stretch. __slots__ keeps ~600k of these small in memory."""
    __slots__ = ("u", "v", "length_m", "time_s", "name", "highway", "geom")

    def __init__(self, u, v, length_m, time_s, name="", highway="road", geom=()):
        self.u, self.v = u, v
        self.length_m = length_m
        self.time_s = time_s            # free-flow travel time (seconds)
        self.name, self.highway = name, highway
        self.geom = geom                # intermediate (lat, lon) points, u and v excluded

    def __getstate__(self):
        return (self.u, self.v, self.length_m, self.time_s, self.name, self.highway, self.geom)

    def __setstate__(self, st):
        self.u, self.v, self.length_m, self.time_s, self.name, self.highway, self.geom = st

    @property
    def speed_kmh(self) -> float:
        return self.length_m / self.time_s * 3.6 if self.time_s > 0 else 0.0


class RoadGraph:
    def __init__(self):
        self.name = "road network"
        self.lat: list[float] = []
        self.lon: list[float] = []
        self.edges: list[Edge] = []
        self.adj: list[list[tuple[int, int]]] = []   # u -> [(v, edge_id)]
        self.radj: list[list[tuple[int, int]]] = []  # v -> [(u, edge_id)]  (reverse graph)
        self.traffic: dict[int, float] = {}          # edge_id -> slowdown factor (>= 1)
        self.blocked: set[int] = set()               # closed edge ids
        self.max_speed_mps = 1.0
        self.raw_nodes = 0                           # OSM points before simplification
        self._reset_caches()

    def _reset_caches(self):
        self._weights: dict[str, list[float]] = {}
        self._index: GridIndex | None = None
        self._geo_index = None
        self._xy = None

    def __getstate__(self):  # don't pickle caches
        d = self.__dict__.copy()
        for k in ("_weights", "_index", "_geo_index", "_xy"):
            d.pop(k, None)
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self._reset_caches()

    # ------------------------------------------------------------------ build
    @property
    def n(self) -> int:
        return len(self.lat)

    def add_node(self, lat: float, lon: float) -> int:
        self.lat.append(lat)
        self.lon.append(lon)
        self.adj.append([])
        self.radj.append([])
        return len(self.lat) - 1

    def add_edge(self, u: int, v: int, length_m: float | None = None, speed_kmh: float = 30,
                 name: str = "", highway: str = "road", geom=None, time_s: float | None = None) -> int:
        if length_m is None:
            length_m = haversine(self.lat[u], self.lon[u], self.lat[v], self.lon[v])
        if time_s is None:
            time_s = length_m / (speed_kmh / 3.6)
        eid = len(self.edges)
        self.edges.append(Edge(u, v, length_m, time_s, name, highway, tuple(geom) if geom else ()))
        self.adj[u].append((v, eid))
        self.radj[v].append((u, eid))
        return eid

    def finalize(self) -> "RoadGraph":
        self._reset_caches()
        self.max_speed_mps = max((e.length_m / e.time_s for e in self.edges if e.time_s > 0), default=1.0)
        return self

    @classmethod
    def from_osm(cls, osm, ignore_oneway: bool = False, simplify: bool = True, log=None) -> "RoadGraph":
        """OSM ways -> raw segment graph -> intersection-only graph -> largest SCC.

        The raw graph (millions of points for a whole city) is kept in compact
        arrays; Python objects are only created for the final, simplified edges."""
        from array import array

        idx: dict[int, int] = {}
        lat, lon = array("d"), array("d")
        eu, ev, elen, etime, eway = array("i"), array("i"), array("d"), array("d"), array("i")
        way_name, way_hw = [], []
        nodes = osm.nodes

        for wi, (tags, refs) in enumerate(osm.ways):
            hw = tags.get("highway", "road")
            way_hw.append(hw)
            way_name.append(tags.get("name:en") or tags.get("name") or hw.replace("_", " "))
            refs = [r for r in refs if r in nodes]
            if len(refs) < 2:
                continue
            speed = (parse_maxspeed(tags.get("maxspeed")) or DEFAULT_SPEED_KMH.get(hw, 25)) / 3.6
            direction = 0 if ignore_oneway else oneway_direction(tags)
            ids = []
            for r in refs:
                i = idx.get(r)
                if i is None:
                    i = idx[r] = len(lat)
                    la, lo = nodes[r]
                    lat.append(la)
                    lon.append(lo)
                ids.append(i)
            for u, v in zip(ids, ids[1:]):
                if u == v:
                    continue
                length = haversine(lat[u], lon[u], lat[v], lon[v])
                t = length / speed
                if direction >= 0:
                    eu.append(u); ev.append(v); elen.append(length); etime.append(t); eway.append(wi)
                if direction <= 0:
                    eu.append(v); ev.append(u); elen.append(length); etime.append(t); eway.append(wi)
        del idx
        n, m = len(lat), len(eu)
        if log:
            log(f"Raw graph: {n:,} OSM points, {m:,} road segments")

        out_e: list[list[int]] = [[] for _ in range(n)]
        in_e: list[list[int]] = [[] for _ in range(n)]
        for i in range(m):
            out_e[eu[i]].append(i)
            in_e[ev[i]].append(i)

        def interior(x: int) -> bool:
            """A point in the middle of a road: exactly two neighbours and
            traffic simply flows through it (not an intersection or dead end)."""
            oe, ie = out_e[x], in_e[x]
            if len(oe) > 2 or len(ie) > 2:
                return False
            outs = {ev[i] for i in oe}
            ins = {eu[i] for i in ie}
            nb = outs | ins
            if len(nb) != 2 or x in nb:
                return False
            if len(oe) == 2 and len(ie) == 2 and outs == ins:   # two-way road
                return True
            return len(oe) == 1 and len(ie) == 1 and outs != ins  # one-way road

        g = cls()
        if not simplify:
            for i in range(n):
                g.add_node(lat[i], lon[i])
            for i in range(m):
                g.add_edge(eu[i], ev[i], elen[i], None, way_name[eway[i]], way_hw[eway[i]], (), etime[i])
        else:
            is_int = bytearray(n)
            for x in range(n):
                if interior(x):
                    is_int[x] = 1
            new_id = {}
            for x in range(n):
                if not is_int[x]:
                    new_id[x] = g.add_node(lat[x], lon[x])
            for u in new_id:
                for e0 in out_e[u]:
                    length, tsec = elen[e0], etime[e0]
                    geom = []
                    prev, cur = u, ev[e0]
                    ok, steps = True, 0
                    while is_int[cur]:
                        steps += 1
                        if steps > n:          # safety net: never loop forever
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
                        g.add_edge(new_id[u], new_id[cur], length, None, way_name[wi], way_hw[wi],
                                   tuple(geom), tsec)
        del out_e, in_e, eu, ev, elen, etime, eway, lat, lon
        g.name = osm.name
        g = g.largest_scc_subgraph()
        g.raw_nodes = n
        g.name = osm.name
        if log:
            log(f"Final graph: {g.n:,} intersections, {len(g.edges):,} directed edges")
        return g.finalize()

    # ----------------------------------------------- strongly connected parts
    def strongly_connected_components(self) -> list[list[int]]:
        """Kosaraju's algorithm (iterative, so it works on millions of nodes). O(V + E)."""
        n = self.n
        visited = [False] * n
        order: list[int] = []
        adj, radj = self.adj, self.radj
        for s in range(n):
            if visited[s]:
                continue
            visited[s] = True
            stack = [(s, 0)]
            while stack:
                u, i = stack[-1]
                if i < len(adj[u]):
                    stack[-1] = (u, i + 1)
                    v = adj[u][i][0]
                    if not visited[v]:
                        visited[v] = True
                        stack.append((v, 0))
                else:
                    stack.pop()
                    order.append(u)
        comp = [-1] * n
        comps: list[list[int]] = []
        for s in reversed(order):
            if comp[s] != -1:
                continue
            cid = len(comps)
            members = [s]
            comp[s] = cid
            stack = [s]
            while stack:
                u = stack.pop()
                for v, _ in radj[u]:
                    if comp[v] == -1:
                        comp[v] = cid
                        members.append(v)
                        stack.append(v)
            comps.append(members)
        return comps

    def largest_scc_subgraph(self) -> "RoadGraph":
        keep = max(self.strongly_connected_components(), key=len)
        new_id = {old: i for i, old in enumerate(keep)}
        g = RoadGraph()
        for old in keep:
            g.add_node(self.lat[old], self.lon[old])
        for e in self.edges:
            nu, nv = new_id.get(e.u), new_id.get(e.v)
            if nu is not None and nv is not None:
                g.add_edge(nu, nv, e.length_m, None, e.name, e.highway, e.geom, e.time_s)
        g.name = self.name
        return g

    # ---------------------------------------------------------------- weights
    def weights(self, metric: str = "time") -> list[float]:
        """Edge weight array for the chosen metric (cached). Blocked roads = inf."""
        if metric not in METRICS:
            raise ValueError(f"metric must be one of {METRICS}")
        w = self._weights.get(metric)
        if w is None:
            if metric == "time":
                tr = self.traffic
                w = [e.time_s * tr.get(i, 1.0) for i, e in enumerate(self.edges)] if tr else \
                    [e.time_s for e in self.edges]
            else:
                w = [e.length_m for e in self.edges]
            for i in self.blocked:
                w[i] = math.inf
            self._weights[metric] = w
        return w

    def projected_xy(self) -> tuple[list[float], list[float]]:
        """Node positions in metres on a flat local projection (equirectangular).
        Straight-line distance there is much cheaper than haversine. Over a
        city-sized area it differs from the true distance by < 0.5 %, so the A*
        heuristic multiplies it by PROJECTION_SAFETY to stay a strict lower bound."""
        if self._xy is None or len(self._xy[0]) != self.n:
            lat0 = math.radians(sum(self.lat) / self.n)
            k = math.pi / 180 * 6_371_008.8
            kx = k * math.cos(lat0)
            self._xy = ([lo * kx for lo in self.lon], [la * k for la in self.lat])
        return self._xy

    def heuristic_scale(self, metric: str) -> float:
        """Multiply straight-line metres by this to get an A* lower bound.
        time: fastest possible travel = straight line at the top speed in the graph."""
        return 1.0 / self.max_speed_mps if metric == "time" else 1.0

    # ---------------------------------------------------------------- traffic
    def _changed(self):
        self._weights.clear()

    def set_traffic(self, eid: int, factor: float):
        """factor >= 1 (2.0 = twice as slow). Never < 1, so the A* heuristic stays admissible."""
        factor = max(1.0, float(factor))
        if factor == 1.0:
            self.traffic.pop(eid, None)
        else:
            self.traffic[eid] = factor
        self._changed()

    def block_edge(self, eid: int):
        self.blocked.add(eid)
        self._changed()

    def _geometry_index(self):
        """Grid index over the shape points of every road -> edge id
        (built on first use; each two-way road is indexed once)."""
        if self._geo_index is None:
            from array import array
            lats, lons, owner = array("d"), array("d"), array("i")
            seen = set()
            for i, e in enumerate(self.edges):
                key = (min(e.u, e.v), max(e.u, e.v), round(e.length_m))
                if key in seen:
                    continue
                seen.add(key)
                pts = [(self.lat[e.u], self.lon[e.u]), *e.geom, (self.lat[e.v], self.lon[e.v])]
                for (a1, b1), (a2, b2) in zip(pts, pts[1:]):
                    lats.append((a1 + a2) / 2)   # middle of each straight piece of road
                    lons.append((b1 + b2) / 2)
                    owner.append(i)
                for a, b in e.geom:
                    lats.append(a)
                    lons.append(b)
                    owner.append(i)
            self._geo_index = (GridIndex(lats, lons), owner)
        return self._geo_index

    def edges_near(self, lat: float, lon: float, radius_m: float) -> list[int]:
        idx, owner = self._geometry_index()
        return sorted({owner[i] for i in idx.within(lat, lon, radius_m)})

    def nearest_edge(self, lat: float, lon: float) -> int:
        idx, owner = self._geometry_index()
        i, _ = idx.nearest(lat, lon)
        return owner[i]

    def twins(self, eid: int) -> list[int]:
        """The edge plus the opposite-direction edge of the same road (if any)."""
        e = self.edges[eid]
        out = [eid]
        for u, k in self.adj[e.v]:
            if u == e.u and k != eid and abs(self.edges[k].length_m - e.length_m) < 1:
                out.append(k)
        return out

    def block_near(self, lat: float, lon: float, radius_m: float = 30) -> list[int]:
        """Close the road at a clicked point (both directions)."""
        eids = self.edges_near(lat, lon, radius_m) or [self.nearest_edge(lat, lon)]
        closed = set()
        for eid in eids:
            closed.update(self.twins(eid))
        self.blocked.update(closed)
        self._changed()
        return sorted(closed)

    def jam_near(self, lat: float, lon: float, radius_m: float = 400, factor: float = 4.0) -> list[int]:
        eids = sorted({t for eid in self.edges_near(lat, lon, radius_m) for t in self.twins(eid)})
        for eid in eids:
            self.traffic[eid] = max(self.traffic.get(eid, 1.0), max(1.0, factor))
        self._changed()
        return eids

    def clear_traffic(self):
        self.traffic.clear()
        self.blocked.clear()
        self._changed()

    # ----------------------------------------------------------------- lookup
    def nearest_node(self, lat: float, lon: float) -> tuple[int, float]:
        if self._index is None:
            self._index = GridIndex(self.lat, self.lon)
        return self._index.nearest(lat, lon)

    def edge_coords(self, eid: int, include_start: bool = True) -> list[list[float]]:
        e = self.edges[eid]
        pts = [[self.lat[e.u], self.lon[e.u]]] if include_start else []
        pts += [list(p) for p in e.geom]
        pts.append([self.lat[e.v], self.lon[e.v]])
        return pts

    def path_coords(self, path_edges: list[int], source: int | None = None) -> list[list[float]]:
        if not path_edges:
            return [[self.lat[source], self.lon[source]]] if source is not None else []
        out = self.edge_coords(path_edges[0])
        for eid in path_edges[1:]:
            out += self.edge_coords(eid, include_start=False)
        return [[round(a, 6), round(b, 6)] for a, b in out]

    def path_stats(self, path_edges: list[int]) -> dict:
        """Length (m) and travel time (s, with current traffic) along a path."""
        tw = self.weights("time")
        names: list[str] = []
        for eid in path_edges:
            nm = self.edges[eid].name
            if not names or names[-1] != nm:
                names.append(nm)
        return {"distance_m": sum(self.edges[e].length_m for e in path_edges),
                "time_s": sum(tw[e] for e in path_edges), "roads": names}

    def bounds(self) -> tuple[float, float, float, float]:
        return (min(self.lat), min(self.lon), max(self.lat), max(self.lon))

    def summary(self) -> dict:
        s, w, n, e = self.bounds()
        return {
            "name": self.name,
            "nodes": self.n,
            "raw_nodes": self.raw_nodes,
            "edges": len(self.edges),
            "road_km": round(sum(e.length_m for e in self.edges) / 1000, 1),
            "max_speed_kmh": round(self.max_speed_mps * 3.6),
            "size_km": [round(haversine(s, w, n, w) / 1000, 1), round(haversine(s, w, s, e) / 1000, 1)],
        }

    # ------------------------------------------------------------ disk cache
    def save(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load(path: str | Path) -> "RoadGraph":
        with open(path, "rb") as f:
            g = pickle.load(f)
        return g.finalize()

    # ------------------------------------------------- synthetic test cities
    @classmethod
    def grid_city(cls, rows: int, cols: int, spacing_m: float = 120, seed: int = 0,
                  origin=(28.6139, 77.2090), removal: float = 0.08) -> "RoadGraph":
        """A synthetic Manhattan-style city (used for the scaling benchmark):
        random speeds, some one-way streets, some missing blocks."""
        rng = random.Random(seed)
        g = cls()
        lat0, lon0 = origin
        dlat = spacing_m / 111_320
        dlon = spacing_m / (111_320 * math.cos(math.radians(lat0)))
        ids = [[g.add_node(lat0 + r * dlat + rng.uniform(-0.15, 0.15) * dlat,
                           lon0 + c * dlon + rng.uniform(-0.15, 0.15) * dlon)
                for c in range(cols)] for r in range(rows)]
        for r in range(rows):
            for c in range(cols):
                for dr, dc in ((0, 1), (1, 0)):
                    rr, cc = r + dr, c + dc
                    if rr >= rows or cc >= cols or rng.random() < removal:
                        continue
                    arterial = (r % 8 == 0 and dr == 0) or (c % 8 == 0 and dc == 0)
                    speed = rng.choice([50, 60]) if arterial else rng.choice([20, 25, 30, 40])
                    u, v = ids[r][c], ids[rr][cc]
                    roll = rng.random()
                    if roll > 0.07:
                        g.add_edge(u, v, None, speed)
                    if roll < 0.07 or roll > 0.14:
                        g.add_edge(v, u, None, speed)
        g = g.largest_scc_subgraph()
        g.name = f"Synthetic grid {rows}x{cols}"
        return g.finalize()
