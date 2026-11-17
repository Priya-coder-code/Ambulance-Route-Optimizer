"""REST API used by the React frontend (all routes under /api)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func, select

from . import geocoding
from .db import session_scope
from .models import Ambulance, Dispatch, Emergency, EmergencyType, Hospital, RoadCondition, Setting
from .planner import PlanError, PlanRequest, plan_dispatch
from .road_network import TRAFFIC_PROFILES
from .seed import FACILITIES

bp = Blueprint("api", __name__, url_prefix="/api")


def rt():
    return current_app.extensions["aro_runtime"]


def cfg():
    return current_app.config["ARO"]


def err(msg, code=400):
    return jsonify({"error": msg}), code


@bp.before_request
def _database_first():
    """Every endpoint except /status needs the database; report problems as JSON, never crash."""
    if request.endpoint == "api.status":
        return None
    r = rt()
    if not r.db_ok:
        r.ensure_db()
        if not r.db_ok:
            return err(r.error or "The database is not reachable.", 503)
    return None


def need_network():
    r = rt()
    r.ensure_loaded(wait=cfg().SERVERLESS)
    if r.net is None:
        return err(r.error or "The road network is still loading - please wait.", 503)
    from .server import sync_network
    sync_network(r.net)   # pick up traffic / closures changed by any server instance
    return None


def body():
    return request.get_json(force=True, silent=True) or {}


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------- status / meta
@bp.get("/status")
def status():
    r = rt()
    c = cfg()
    # on Vercel this request does the loading itself; locally a background thread does it
    r.ensure_loaded(wait=c.SERVERLESS)
    from .geo import square_bbox

    s, w, n, e = square_bbox(*c.AREA_CENTER, c.AREA_SIZE_KM)
    out = {"status": r.status, "log": r.log[-12:], "error": r.error,
           "db_mode": c.DB_MODE,
           "area": {"name": c.AREA_NAME, "center": list(c.AREA_CENTER), "size_km": c.AREA_SIZE_KM,
                    "bounds": [[s, w], [n, e]]}}
    if r.net is not None:
        out["network"] = r.net.summary()
    return jsonify(out)


@bp.get("/meta")
def meta():
    with session_scope() as s:
        types = [t.to_dict() for t in s.scalars(select(EmergencyType).order_by(EmergencyType.sort))]
    return jsonify({
        "emergency_types": types,
        "facilities": FACILITIES,
        "traffic_profiles": {k: v["label"] for k, v in TRAFFIC_PROFILES.items()},
        "severities": {"critical": "Critical", "serious": "Serious", "stable": "Stable"},
    })


# -------------------------------------------------------------------- geocoding
@bp.get("/geocode")
def geocode():
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return err("Type at least 3 characters")
    try:
        return jsonify({"results": geocoding.search(cfg(), q)})
    except Exception as e:
        return err(f"Address search failed ({e}). You can still click on the map.", 502)


@bp.get("/reverse")
def reverse():
    try:
        lat, lon = float(request.args["lat"]), float(request.args["lon"])
        return jsonify({"address": geocoding.reverse(cfg(), lat, lon)})
    except Exception:
        return jsonify({"address": None})


# --------------------------------------------------------------------- fleet
@bp.get("/ambulances")
def list_ambulances():
    with session_scope() as s:
        return jsonify([a.to_dict() for a in s.scalars(select(Ambulance).order_by(Ambulance.code))])


@bp.post("/ambulances")
def add_ambulance():
    b = body()
    with session_scope() as s:
        n = (s.scalar(select(func.count(Ambulance.id))) or 0) + 1
        code = b.get("code") or f"AMB-{n:03d}"
        while s.scalar(select(Ambulance).where(Ambulance.code == code)):
            n += 1
            code = f"AMB-{n:03d}"
        a = Ambulance(code=code, kind=b.get("kind", "BLS"), lat=float(b["lat"]), lon=float(b["lon"]),
                      base_name=b.get("base_name") or "Added by operator", status="available")
        s.add(a)
        s.flush()
        return jsonify(a.to_dict()), 201


@bp.patch("/ambulances/<int:aid>")
def update_ambulance(aid):
    b = body()
    with session_scope() as s:
        a = s.get(Ambulance, aid)
        if not a:
            return err("not found", 404)
        if "status" in b:
            if b["status"] not in ("available", "busy", "maintenance"):
                return err("bad status")
            a.status = b["status"]
        if "kind" in b and b["kind"] in ("ALS", "BLS"):
            a.kind = b["kind"]
        for k in ("lat", "lon"):
            if k in b:
                setattr(a, k, float(b[k]))
        return jsonify(a.to_dict())


@bp.delete("/ambulances/<int:aid>")
def delete_ambulance(aid):
    with session_scope() as s:
        a = s.get(Ambulance, aid)
        if not a:
            return err("not found", 404)
        if s.scalar(select(func.count(Dispatch.id)).where(Dispatch.ambulance_id == aid)):
            a.status = "maintenance"   # keep history intact
        else:
            s.delete(a)
        return jsonify({"ok": True})


# ------------------------------------------------------------------ hospitals
@bp.get("/hospitals")
def list_hospitals():
    with session_scope() as s:
        return jsonify([h.to_dict() for h in s.scalars(select(Hospital).order_by(Hospital.name))])


@bp.post("/hospitals")
def add_hospital():
    b = body()
    with session_scope() as s:
        h = Hospital(name=b.get("name") or "New hospital", lat=float(b["lat"]), lon=float(b["lon"]),
                     facilities=[f for f in b.get("facilities", ["emergency"]) if f in FACILITIES],
                     beds_available=int(b.get("beds_available", 10)), source="manual")
        s.add(h)
        s.flush()
        return jsonify(h.to_dict()), 201


@bp.patch("/hospitals/<int:hid>")
def update_hospital(hid):
    b = body()
    with session_scope() as s:
        h = s.get(Hospital, hid)
        if not h:
            return err("not found", 404)
        if "facilities" in b:
            h.facilities = [f for f in FACILITIES if f in set(b["facilities"])]
        if "beds_available" in b:
            h.beds_available = max(0, int(b["beds_available"]))
        if "is_active" in b:
            h.is_active = bool(b["is_active"])
        if "name" in b and b["name"]:
            h.name = b["name"][:200]
        return jsonify(h.to_dict())


# ------------------------------------------------------------------- planning
@bp.post("/plan")
def plan():
    blocked = need_network()
    if blocked:
        return blocked
    b = body()
    try:
        lat, lon = float(b["lat"]), float(b["lon"])
    except (KeyError, TypeError, ValueError):
        return err("Choose the patient location first")
    severity = b.get("severity", "serious")
    if severity not in ("critical", "serious", "stable"):
        return err("bad severity")
    with session_scope() as s:
        et = s.get(EmergencyType, b.get("emergency_type") or "")
        if not et:
            return err("Choose the emergency type")
        ambulances = [a.to_dict() for a in s.scalars(select(Ambulance))]
        hospitals = [h.to_dict() for h in s.scalars(select(Hospital))]
        try:
            result = plan_dispatch(rt().net, PlanRequest(lat, lon, et.to_dict(), severity,
                                                         bool(b.get("show_search", True))),
                                   ambulances, hospitals, cfg().HOSPITAL_CANDIDATES)
        except PlanError as e:
            return err(str(e), 422)
        # re-planning an existing, not yet dispatched case (e.g. after a road closure) updates it in place
        d = None
        if b.get("dispatch_id"):
            d = s.get(Dispatch, int(b["dispatch_id"]))
            if d is not None and d.status != "planned":
                d = None
        em = d.emergency if d else Emergency(lat=lat, lon=lon, emergency_type=et.code)
        em.patient_name, em.phone = (b.get("patient_name") or None), (b.get("phone") or None)
        em.address, em.notes = (b.get("address") or None), (b.get("notes") or None)
        em.lat, em.lon, em.emergency_type, em.severity = lat, lon, et.code, severity
        s.add(em)
        s.flush()
        d = d or Dispatch(emergency_id=em.id, status="planned")
        d.ambulance_id, d.hospital_id = result["ambulance"]["id"], result["hospital"]["id"]
        d.eta_to_patient_s = result["ambulance"]["eta_s"]
        d.distance_to_patient_m = result["ambulance"]["distance_m"]
        d.eta_to_hospital_s = result["hospital"]["eta_s"]
        d.distance_to_hospital_m = result["hospital"]["distance_m"]
        d.total_eta_s, d.total_distance_m = result["total"]["eta_s"], result["total"]["distance_m"]
        d.route_to_patient, d.route_to_hospital = result["ambulance"]["route"], result["hospital"]["route"]
        d.details = ({k: result[k] for k in ("patient", "candidates", "algorithm", "warnings")}
                     | {"ambulance": {k: v for k, v in result["ambulance"].items() if k != "route"},
                        "hospital": {k: v for k, v in result["hospital"].items() if k != "route"},
                        "emergency_label": et.label})
        s.add(d)
        s.flush()
        s.refresh(em)
        result["dispatch_id"] = d.id
        result["status"] = d.status
        result["emergency"] = em.to_dict()
        return jsonify(result)


@bp.post("/dispatches/<int:did>/confirm")
def confirm(did):
    with session_scope() as s:
        d = s.get(Dispatch, did)
        if not d:
            return err("not found", 404)
        if d.status != "planned":
            return err(f"dispatch is already {d.status}")
        if d.ambulance.status != "available":
            return err(f"{d.ambulance.code} is no longer available - plan again", 409)
        d.status, d.dispatched_at = "dispatched", _now()
        d.ambulance.status = "busy"
        if d.hospital and d.hospital.beds_available > 0:
            d.hospital.beds_available -= 1
        return jsonify(d.to_dict())


@bp.post("/dispatches/<int:did>/complete")
def complete(did):
    """Patient delivered: the ambulance is free again, now parked at the hospital."""
    with session_scope() as s:
        d = s.get(Dispatch, did)
        if not d:
            return err("not found", 404)
        if d.status != "dispatched":
            return err("only a dispatched case can be completed")
        d.status, d.completed_at = "completed", _now()
        a = d.ambulance
        a.status = "available"
        if d.hospital:
            a.lat, a.lon, a.base_name = d.hospital.lat, d.hospital.lon, d.hospital.name
        return jsonify(d.to_dict())


@bp.post("/dispatches/<int:did>/cancel")
def cancel(did):
    with session_scope() as s:
        d = s.get(Dispatch, did)
        if not d:
            return err("not found", 404)
        if d.status in ("completed", "cancelled"):
            return err(f"dispatch is already {d.status}")
        if d.status == "dispatched":
            d.ambulance.status = "available"
            if d.hospital:
                d.hospital.beds_available += 1
        d.status = "cancelled"
        return jsonify(d.to_dict())


@bp.get("/dispatches")
def list_dispatches():
    limit = min(200, int(request.args.get("limit", 50)))
    with session_scope() as s:
        rows = s.scalars(select(Dispatch).order_by(Dispatch.id.desc()).limit(limit))
        return jsonify([d.to_dict(routes=False) for d in rows])


@bp.get("/dispatches/<int:did>")
def get_dispatch(did):
    with session_scope() as s:
        d = s.get(Dispatch, did)
        if not d:
            return err("not found", 404)
        return jsonify(d.to_dict())


@bp.get("/stats")
def stats():
    with session_scope() as s:
        by_status = dict(s.execute(select(Ambulance.status, func.count()).group_by(Ambulance.status)).all())
        since = _now() - timedelta(hours=24)
        recent = s.scalars(select(Dispatch).where(Dispatch.created_at >= since,
                                                  Dispatch.status.in_(["dispatched", "completed"]))).all()
        avg = sum(d.eta_to_patient_s for d in recent) / len(recent) if recent else None
        return jsonify({"ambulances": by_status, "dispatches_24h": len(recent), "avg_response_s": avg,
                        "active_cases": s.scalar(select(func.count(Dispatch.id))
                                                 .where(Dispatch.status == "dispatched"))})


# -------------------------------------------------------------- road network
def _conditions(s):
    return [c.to_dict() for c in s.scalars(select(RoadCondition).where(RoadCondition.active.is_(True))
                                           .order_by(RoadCondition.id))]


def _condition_shapes(net):
    seen, closed, jam = set(), [], []
    for eid in sorted(net.blocked):
        key = tuple(sorted((net.e_u[eid], net.e_v[eid])))
        if key not in seen:
            seen.add(key)
            closed.append(net.edge_shape(eid))
    seen = set()
    for eid in sorted(net.congestion):
        key = tuple(sorted((net.e_u[eid], net.e_v[eid])))
        if key not in seen:
            seen.add(key)
            jam.append(net.edge_shape(eid))
    return {"closed": closed, "congested": jam}


@bp.get("/road-conditions")
def road_conditions():
    with session_scope() as s:
        out = {"conditions": _conditions(s)}
    if need_network() is None:
        net = rt().net
        out["shapes"] = _condition_shapes(net)
        out["traffic_profile"] = net.profile
    return jsonify(out)


@bp.post("/road-conditions")
def add_condition():
    blocked = need_network()
    if blocked:
        return blocked
    b = body()
    kind = b.get("kind")
    if kind not in ("closure", "congestion"):
        return err("kind must be closure or congestion")
    with session_scope() as s:
        c = RoadCondition(kind=kind, lat=float(b["lat"]), lon=float(b["lon"]),
                          radius_m=float(b.get("radius_m", 30 if kind == "closure" else 400)),
                          factor=float(b.get("factor", 1.0 if kind == "closure" else 3.0)),
                          note=b.get("note") or ("Road closed" if kind == "closure" else "Heavy traffic"))
        s.add(c)
    need_network()   # re-applies everything from the database
    net = rt().net
    with session_scope() as s:
        return jsonify({"conditions": _conditions(s),
                        "effect": {"closed_edges": len(net.blocked), "congested_edges": len(net.congestion)},
                        "shapes": _condition_shapes(net)}), 201


@bp.delete("/road-conditions/<int:cid>")
def delete_condition(cid):
    with session_scope() as s:
        c = s.get(RoadCondition, cid)
        if c:
            c.active = False
    with session_scope() as s:
        out = {"conditions": _conditions(s)}
    if need_network() is None:
        net = rt().net
        out["effect"] = {"closed_edges": len(net.blocked), "congested_edges": len(net.congestion)}
        out["shapes"] = _condition_shapes(net)
    return jsonify(out)


@bp.post("/traffic-profile")
def traffic_profile():
    blocked = need_network()
    if blocked:
        return blocked
    p = body().get("profile")
    if p not in TRAFFIC_PROFILES:
        return err("unknown profile")
    with session_scope() as s:
        row = s.get(Setting, "traffic_profile") or Setting(key="traffic_profile", value=p)
        row.value = p
        s.add(row)
    need_network()
    return jsonify({"traffic_profile": p})


@bp.get("/roads")
def roads():
    blocked = need_network()
    if blocked:
        return blocked
    try:
        s_, w, n, e = (float(request.args[k]) for k in ("s", "w", "n", "e"))
    except (KeyError, ValueError):
        return err("need s, w, n, e")
    return jsonify({"roads": rt().net.shapes_in_view(s_, w, n, e)})
