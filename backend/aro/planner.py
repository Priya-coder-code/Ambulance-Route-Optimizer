"""The decision pipeline from the project document.

Input : patient location, emergency type, severity
Output: 1. which ambulance should respond
        2. its route to the patient
        3. which suitable hospital the patient should go to
        4. the route from the patient to that hospital
        5. the estimated total travel time

Step A - ambulance selection
    For every available ambulance (of the right type) run Dijkstra from the
    ambulance to the patient and keep the one with the minimum travel time.
    Two exact speed-ups (they never change the answer):
      * lower bound  : straight-line distance / top speed can never be beaten.
                       Ambulances are tried in order of this bound; once the
                       bound is worse than the best time found, all remaining
                       ambulances are skipped without running Dijkstra.
      * cutoff       : each Dijkstra stops as soon as it has looked at every
                       place reachable faster than the current best time.

Step B - hospital selection
    Hospitals are filtered by the facilities the emergency type needs (and free
    beds). ONE Dijkstra from the patient settles nodes in order of travel time,
    so the hospitals are found fastest-first; it stops after the K fastest
    suitable hospitals. Unsuitable hospitals that are closer are reported, to
    show why the nearest hospital is not always the right one.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .dijkstra import dijkstra
from .geo import haversine

MAX_EXPLORED_POINTS = 25_000


@dataclass
class PlanRequest:
    lat: float
    lon: float
    emergency_type: dict          # EmergencyType.to_dict()
    severity: str = "serious"     # critical | serious | stable
    show_search: bool = True      # return explored nodes for the map


class PlanError(Exception):
    pass


def _sample(seq, k):
    if len(seq) <= k:
        return list(seq)
    step = len(seq) / k
    return [seq[int(i * step)] for i in range(k)]


def plan_dispatch(net, req: PlanRequest, ambulances: list[dict], hospitals: list[dict],
                  hospital_candidates: int = 5) -> dict:
    t_start = time.perf_counter()
    warnings: list[str] = []
    et = req.emergency_type
    with net.lock:
        weights = net.weights()
        adj = net.adj
        vmax = net.max_speed_mps
        profile = net.profile

        patient, snap_m = net.nearest_open_node(req.lat, req.lon)
        if snap_m > 1500:
            raise PlanError("The patient location is outside the service area (more than 1.5 km from any road).")
        if snap_m > 300:
            warnings.append(f"Patient location is {snap_m:.0f} m from the nearest drivable road.")

        # ------------------------------------------------ Step A: ambulance
        available = [a for a in ambulances if a["status"] == "available"]
        if not available:
            raise PlanError("No ambulance is available right now.")
        needs_als = et["needs_als"] or req.severity == "critical"
        pool = [a for a in available if a["kind"] == "ALS"] if needs_als else available
        excluded_type = []
        if needs_als:
            excluded_type = [a for a in available if a["kind"] != "ALS"]
            if not pool:
                warnings.append("No advanced life-support (ALS) ambulance is free - using a basic (BLS) one.")
                pool, excluded_type = available, []

        runs, settled_total, relaxed_total = 0, 0, 0

        def select_ambulance(patient):
            """Dijkstra from each candidate ambulance to `patient`; keep the fastest."""
            nonlocal runs, settled_total, relaxed_total
            cands = []
            for a in pool:
                node, _ = net.nearest_open_node(a["lat"], a["lon"])
                straight = haversine(net.lat[node], net.lon[node], net.lat[patient], net.lon[patient])
                cands.append({"amb": a, "node": node, "straight_m": straight, "lower_bound_s": straight / vmax})
            cands.sort(key=lambda c: c["lower_bound_s"])   # most promising first
            best, best_time, pruned = None, math.inf, 0
            for c in cands:
                if c["lower_bound_s"] > best_time:
                    c["status"] = "pruned"
                    c["note"] = "skipped: even in a straight line at top speed it is slower than the best"
                    pruned += 1
                    continue
                r = dijkstra(adj, weights, c["node"], targets={patient}, cutoff=best_time, record=req.show_search)
                runs += 1
                settled_total += r.settled
                relaxed_total += r.relaxed
                c["settled"] = r.settled
                if r.reached:
                    t = r.dist[patient]
                    c["eta_s"], c["status"] = t, "reachable"
                    if t < best_time:
                        best_time, best = t, (c, r)
                else:
                    c["status"] = "slower" if r.stopped_by_cutoff else "unreachable"
                    c["note"] = ("stopped early: cannot beat the best time" if r.stopped_by_cutoff
                                 else "no open road to the patient")
            return cands, best, pruned

        cands, best, pruned = select_ambulance(patient)
        if best is None:
            # the patient's street may be cut off by a closure: use the nearest point the ambulance can reach
            near = sorted((haversine(req.lat, req.lon, net.lat[i], net.lon[i]), i)
                          for i in net._node_index.within(req.lat, req.lon, 800) if i != patient)
            for dist_m, alt in near[:8]:
                cands, best, pruned = select_ambulance(alt)
                if best is not None:
                    patient, snap_m = alt, dist_m
                    warnings.append(f"The patient's street is cut off by a road closure - the ambulance will stop "
                                    f"at the nearest reachable point, {dist_m:.0f} m away.")
                    break
        if best is None:
            raise PlanError("No ambulance can reach the patient (roads closed?).")
        chosen, r_amb = best
        chosen["status"] = "selected"
        amb_nodes, amb_edges = r_amb.path_to(patient)
        leg1 = net.route_summary(amb_edges)

        # ------------------------------------------------- Step B: hospital
        required = set(et["required_facilities"])

        def why_not(h):
            missing = [f for f in et["required_facilities"] if f not in (h["facilities"] or [])]
            if missing:
                return "no " + ", ".join(missing)
            if h["beds_available"] <= 0:
                return "no free beds"
            if not h["is_active"]:
                return "inactive"
            return None

        active = [h for h in hospitals if h["is_active"]]
        suitable = [h for h in active if why_not(h) is None]
        fallback_used = False
        if not suitable:
            fallback_used = True
            suitable = [h for h in active if "emergency" in (h["facilities"] or []) and h["beds_available"] > 0] or active
            warnings.append("No hospital has all the facilities this emergency needs - "
                            "sending to the nearest hospital with an emergency department.")
        if not suitable:
            raise PlanError("No hospitals are available in the database.")
        suitable_ids = {h["id"] for h in suitable}

        by_node: dict[int, list[dict]] = {}
        for h in active:
            node, _ = net.nearest_open_node(h["lat"], h["lon"])
            by_node.setdefault(node, []).append(h)
        suitable_nodes = {n for n, hs in by_node.items() if any(h["id"] in suitable_ids for h in hs)}

        # ONE Dijkstra from the patient. Nodes are settled in order of travel time, so
        # hospitals are reached fastest-first. All hospital nodes are targets (so closer
        # but unsuitable ones can be reported); stop after K suitable ones.
        found = [0]

        def enough(node):
            if node in suitable_nodes:
                found[0] += 1
            return found[0] >= hospital_candidates

        r_hosp = dijkstra(adj, weights, patient, targets=set(by_node), record=req.show_search,
                          stop_condition=enough)
        runs += 1
        settled_total += r_hosp.settled
        relaxed_total += r_hosp.relaxed
        hosp_rows, skipped_rows = [], []
        for node in r_hosp.reached:
            for h in by_node[node]:
                row = {"hospital": h, "node": node, "eta_s": r_hosp.dist[node]}
                if h["id"] in suitable_ids:
                    hosp_rows.append(row)
                elif not hosp_rows and len(skipped_rows) < 5:
                    row["reason"] = why_not(h)
                    skipped_rows.append(row)
        hosp_rows = hosp_rows[:hospital_candidates]
        if not hosp_rows:
            raise PlanError("No suitable hospital can be reached from the patient (roads closed?).")
        h_best = hosp_rows[0]
        hosp_nodes, hosp_edges = r_hosp.path_to(h_best["node"])
        leg2 = net.route_summary(hosp_edges)

        compute_ms = (time.perf_counter() - t_start) * 1000

        def amb_row(c):
            a = c["amb"]
            return {"id": a["id"], "code": a["code"], "kind": a["kind"], "base_name": a["base_name"],
                    "lat": a["lat"], "lon": a["lon"], "straight_m": round(c["straight_m"]),
                    "eta_s": c.get("eta_s"), "status": c.get("status", "pruned"), "note": c.get("note"),
                    "settled": c.get("settled", 0)}

        def hosp_row(row, selected=False):
            h = row["hospital"]
            return {"id": h["id"], "name": h["name"], "lat": h["lat"], "lon": h["lon"],
                    "facilities": h["facilities"], "beds_available": h["beds_available"],
                    "eta_s": row["eta_s"], "selected": selected, "reason": row.get("reason")}

        explored_amb = _sample(r_amb.explored, MAX_EXPLORED_POINTS)
        explored_hosp = _sample(r_hosp.explored[:r_hosp.explored.index(h_best["node"]) + 1]
                                if h_best["node"] in r_hosp.explored else r_hosp.explored, MAX_EXPLORED_POINTS)

        return {
            "patient": {"lat": req.lat, "lon": req.lon, "road_lat": net.lat[patient], "road_lon": net.lon[patient],
                        "snap_m": round(snap_m, 1)},
            "ambulance": {**amb_row(chosen), "eta_s": leg1["time_s"], "distance_m": leg1["distance_m"],
                          "route": net.route_coords(amb_edges, patient), "roads": leg1["roads"][:15]},
            "hospital": {**hosp_row(h_best, True), "eta_s": leg2["time_s"], "distance_m": leg2["distance_m"],
                         "route": net.route_coords(hosp_edges, patient), "roads": leg2["roads"][:15]},
            "total": {"eta_s": leg1["time_s"] + leg2["time_s"], "distance_m": leg1["distance_m"] + leg2["distance_m"]},
            "candidates": {
                "ambulances": [amb_row(c) for c in cands],
                "excluded_ambulances": [{"id": a["id"], "code": a["code"], "kind": a["kind"],
                                         "reason": "basic (BLS) ambulance - this case needs ALS"} for a in excluded_type],
                "hospitals": [hosp_row(r, i == 0) for i, r in enumerate(hosp_rows)],
                "skipped_hospitals": [hosp_row(r) for r in skipped_rows],
                "suitable_hospital_count": len(suitable),
                "required_facilities": sorted(required),
                "fallback_hospital_rule": fallback_used,
            },
            "algorithm": {
                "name": "Dijkstra (binary heap)",
                "dijkstra_runs": runs,
                "ambulances_considered": len(cands),
                "ambulances_pruned": pruned,
                "nodes_settled": settled_total,
                "edges_relaxed": relaxed_total,
                "compute_ms": round(compute_ms, 1),
                "graph_nodes": net.n,
                "graph_edges": net.m,
                "traffic_profile": profile,
                "needs_als": needs_als,
            },
            "search": {
                "ambulance": [[round(net.lat[i], 5), round(net.lon[i], 5)] for i in explored_amb],
                "hospital": [[round(net.lat[i], 5), round(net.lon[i], 5)] for i in explored_hosp],
            } if req.show_search else None,
            "warnings": warnings,
        }
