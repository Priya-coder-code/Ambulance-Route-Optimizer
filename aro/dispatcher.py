"""Ambulance dispatch on top of the shortest-path algorithms.

For an emergency at point P:
  Leg 1  nearest *available* ambulance -> P
         = one search on the REVERSED graph starting at P, stopping at the first
           ambulance base reached (handles one-way streets correctly).
  Leg 2  P -> nearest hospital
         = one multi-target search on the normal graph.

Each leg is solved with both Dijkstra and A* so they can be compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .algorithms import SearchResult, run


@dataclass
class Place:
    id: int
    kind: str          # "hospital" | "ambulance"
    name: str
    lat: float
    lon: float
    node: int          # snapped road-graph node
    snap_m: float      # distance from the real location to the road
    available: bool = True

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name, "lat": self.lat, "lon": self.lon,
                "node": self.node, "snap_m": round(self.snap_m, 1), "available": self.available}


@dataclass
class Dispatcher:
    graph: object
    hospitals: list[Place] = field(default_factory=list)
    ambulances: list[Place] = field(default_factory=list)
    _next_id: int = 0

    # -------------------------------------------------------------- setup
    def _place(self, kind: str, name: str, lat: float, lon: float) -> Place:
        node, snap = self.graph.nearest_node(lat, lon)
        self._next_id += 1
        return Place(self._next_id, kind, name, lat, lon, node, snap)

    def add_hospital(self, name: str, lat: float, lon: float) -> Place:
        p = self._place("hospital", name, lat, lon)
        self.hospitals.append(p)
        return p

    def add_ambulance(self, name: str, lat: float, lon: float) -> Place:
        p = self._place("ambulance", name, lat, lon)
        self.ambulances.append(p)
        return p

    @classmethod
    def from_pois(cls, graph, pois: list[dict], ambulance_at_each_hospital: bool = True,
                  max_snap_m: float = 1500) -> "Dispatcher":
        """Hospitals / ambulance stations from OSM. By default each hospital also
        has one ambulance parked at it."""
        d = cls(graph)
        s, w, n, e = graph.bounds()
        seen = set()
        for p in pois:
            if not (s <= p["lat"] <= n and w <= p["lon"] <= e):
                continue
            key = (p["kind"], round(p["lat"], 4), round(p["lon"], 4))
            if key in seen:
                continue
            seen.add(key)
            if p["kind"] == "hospital":
                h = d.add_hospital(p["name"], p["lat"], p["lon"])
                if h.snap_m > max_snap_m:
                    d.hospitals.pop()
                    continue
                if ambulance_at_each_hospital:
                    d.add_ambulance(f"Ambulance @ {p['name']}", p["lat"], p["lon"])
            elif p["kind"] in ("ambulance_station", "ambulance"):
                a = d.add_ambulance(p["name"], p["lat"], p["lon"])
                if a.snap_m > max_snap_m:
                    d.ambulances.pop()
        return d

    def reset_ambulances(self):
        for a in self.ambulances:
            a.available = True

    # ----------------------------------------------------------- dispatch
    def nearest_ambulance(self, incident_node: int, algorithm: str, metric: str = "time",
                          record: bool = True) -> tuple[SearchResult, Place | None]:
        free = [a for a in self.ambulances if a.available]
        if not free:
            raise RuntimeError("No ambulance available")
        targets = {a.node for a in free}
        res = run(algorithm, self.graph, incident_node, targets, metric, reverse=True, record=record)
        # the reverse search found incident <- ... <- ambulance; flip it
        res.path = list(reversed(res.path))
        res.path_edges = list(reversed(res.path_edges))
        amb = next((a for a in free if a.node == res.target), None)
        return res, amb

    def nearest_hospital(self, incident_node: int, algorithm: str, metric: str = "time",
                         record: bool = True) -> tuple[SearchResult, Place | None]:
        if not self.hospitals:
            raise RuntimeError("No hospitals on the map - add one first")
        targets = {h.node for h in self.hospitals}
        res = run(algorithm, self.graph, incident_node, targets, metric, record=record)
        hosp = next((h for h in self.hospitals if h.node == res.target), None)
        return res, hosp

    def dispatch(self, lat: float, lon: float, metric: str = "time",
                 algorithms=("dijkstra", "astar"), mark_busy: bool = False, record: bool = True) -> dict:
        incident, snap = self.graph.nearest_node(lat, lon)
        out = {"incident": {"lat": lat, "lon": lon, "node": incident, "snap_m": snap}, "legs": {}}
        chosen_amb = None
        for algo in algorithms:
            r1, amb = self.nearest_ambulance(incident, algo, metric, record)
            r2, hosp = self.nearest_hospital(incident, algo, metric, record)
            out["legs"][algo] = {"to_patient": (r1, amb), "to_hospital": (r2, hosp)}
            chosen_amb = chosen_amb or amb
        if mark_busy and chosen_amb:
            chosen_amb.available = False
        return out
