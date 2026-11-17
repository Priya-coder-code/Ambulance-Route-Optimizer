"""Initial data.

  * emergency_types : fixed list below (what each emergency needs)
  * hospitals       : REAL hospitals from OpenStreetMap (amenity=hospital) in the
                      service area. OSM has no facility data, so facilities and
                      free beds are SIMULATED (deterministic, editable in the UI).
  * ambulances      : SIMULATED fleet parked at hospitals and standby points on
                      real roads.
"""

from __future__ import annotations

import hashlib
import random

from sqlalchemy import func, select

from .geo import haversine, square_bbox
from .models import Ambulance, EmergencyType, Hospital

FACILITIES = {
    "emergency": "24x7 Emergency",
    "trauma": "Trauma centre",
    "cardiac": "Cardiac care",
    "burn": "Burn unit",
    "neuro": "Neuro / stroke",
    "maternity": "Maternity",
    "pediatric": "Paediatrics",
    "icu": "ICU",
}

EMERGENCY_TYPES = [
    # code, label, description, required hospital facilities, needs advanced (ALS) ambulance
    ("accident", "Road accident / trauma", "Injuries from a crash or fall", ["emergency", "trauma"], True),
    ("heart", "Heart attack / chest pain", "Suspected cardiac emergency", ["emergency", "cardiac"], True),
    ("burn", "Burn injury", "Fire, chemical or electrical burns", ["emergency", "burn"], True),
    ("stroke", "Stroke / unconscious", "Sudden weakness, slurred speech, fainting", ["emergency", "neuro"], True),
    ("breathing", "Breathing difficulty", "Asthma attack, choking, severe breathlessness", ["emergency", "icu"], True),
    ("pregnancy", "Pregnancy / delivery", "Labour or pregnancy complications", ["emergency", "maternity"], False),
    ("child", "Child emergency", "Medical emergency in a child", ["emergency", "pediatric"], False),
    ("poisoning", "Poisoning / overdose", "Swallowed poison, drugs, snake bite", ["emergency", "icu"], False),
    ("general", "Other / general", "Any other medical emergency", ["emergency"], False),
]

# Keyword rules for real hospital names (case-insensitive)
FULL_SERVICE = ("aiims", "all india institute", "safdarjung", "medical college", "ram manohar", "lohia", "lok nayak",
                "guru teg", "gtb", "ganga ram", "max ", "max super", "fortis", "apollo", "medanta", "blk", "trauma",
                "institute of medical", "manipal", "artemis", "deen dayal", "hindu rao", "lal bahadur")
NO_EMERGENCY = ("eye", "dental", "dent", "ent ", "skin", "derma", "cancer", "oncology", "ayurved", "homeo",
                "unani", "physio", "ivf", "fertility", "cosmetic", "veterinary", "animal", "rehab", "psychiatr")
RULES = [
    (("heart", "cardiac", "cardio", "escorts"), ["cardiac", "icu"]),
    (("burn",), ["burn"]),
    (("neuro", "brain", "spine"), ["neuro", "icu"]),
    (("maternity", "women", "mother", "matri", "nursing home"), ["maternity"]),
    (("child", "children", "kalawati", "kids", "pediatric", "paediatric"), ["pediatric"]),
    (("orthop", "bone", "accident"), ["trauma"]),
]

# Used ONLY if OpenStreetMap cannot be reached while seeding (approximate positions in Delhi).
FALLBACK_DELHI_HOSPITALS = [
    ("AIIMS New Delhi", 28.5672, 77.2100),
    ("Safdarjung Hospital", 28.5683, 77.2058),
    ("Ram Manohar Lohia Hospital", 28.6256, 77.2005),
    ("Lok Nayak Hospital", 28.6390, 77.2390),
    ("G B Pant Hospital", 28.6395, 77.2370),
    ("Sir Ganga Ram Hospital", 28.6383, 77.1898),
    ("Kalawati Saran Children's Hospital", 28.6340, 77.2080),
    ("Guru Teg Bahadur Hospital", 28.6858, 77.3100),
    ("Indraprastha Apollo Hospital", 28.5411, 77.2833),
    ("Fortis Escorts Heart Institute", 28.5610, 77.2735),
    ("Max Super Speciality Hospital, Saket", 28.5276, 77.2117),
    ("BLK-Max Super Speciality Hospital", 28.6440, 77.1805),
]


def _rng_for(key: str, seed: int) -> random.Random:
    return random.Random(int(hashlib.md5(f"{seed}:{key}".encode()).hexdigest()[:12], 16))


def simulate_facilities(name: str, key: str, seed: int = 42) -> tuple[list[str], int]:
    """Plausible facilities + free beds for a real hospital (deterministic per hospital)."""
    rng = _rng_for(key, seed)
    low = f" {name.lower()} "
    if any(k in low for k in NO_EMERGENCY):
        return [], rng.randint(0, 10)
    if any(k in low for k in FULL_SERVICE):
        return list(FACILITIES), rng.randint(8, 40)
    fac = set()
    if rng.random() < 0.85:
        fac.add("emergency")
    for words, adds in RULES:
        if any(w in low for w in words):
            fac.update(adds)
            fac.add("emergency")
    for f, p in (("trauma", 0.30), ("cardiac", 0.30), ("burn", 0.10), ("neuro", 0.20),
                 ("maternity", 0.45), ("pediatric", 0.40), ("icu", 0.50)):
        if rng.random() < p:
            fac.add(f)
    beds = rng.choice([0, 0, 2, 4, 6, 8, 10, 12, 15, 20, 25])
    return [f for f in FACILITIES if f in fac], beds


def seed_emergency_types(session):
    for i, (code, label, desc, req, als) in enumerate(EMERGENCY_TYPES):
        et = session.get(EmergencyType, code) or EmergencyType(code=code)
        et.label, et.description, et.required_facilities, et.needs_als, et.sort = label, desc, req, als, i
        session.add(et)


def fetch_osm_hospitals(cfg, log=print) -> list[dict]:
    """Real hospitals (amenity=hospital / healthcare=hospital) inside the service area."""
    import osmnx as ox

    s, w, n, e = square_bbox(*cfg.AREA_CENTER, cfg.AREA_SIZE_KM)
    log("Fetching hospitals from OpenStreetMap ...")
    gdf = ox.features_from_bbox((w, s, e, n), tags={"amenity": "hospital", "healthcare": "hospital"})
    out, seen = [], set()
    for (etype, eid), row in gdf.iterrows():
        name = row.get("name:en") if isinstance(row.get("name:en"), str) else row.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        pt = row.geometry.representative_point()
        lat, lon = pt.y, pt.x
        key = (name.strip().lower(), round(lat, 3), round(lon, 3))
        if key in seen:
            continue
        seen.add(key)
        addr = ", ".join(str(row.get(k)) for k in ("addr:housenumber", "addr:street", "addr:suburb", "addr:city")
                         if isinstance(row.get(k), str))
        phone = row.get("phone") if isinstance(row.get("phone"), str) else None
        out.append({"name": name.strip()[:200], "lat": lat, "lon": lon, "osm_id": f"{etype}/{eid}",
                    "address": addr or None, "phone": phone})
    log(f"  {len(out)} named hospitals found")
    return out


def seed_hospitals(session, cfg, log=print, hospitals: list[dict] | None = None) -> int:
    if session.scalar(select(func.count(Hospital.id))):
        return 0
    source = "osm"
    if hospitals is None:
        try:
            hospitals = fetch_osm_hospitals(cfg, log)
        except Exception as e:  # offline
            log(f"  could not fetch hospitals from OpenStreetMap ({e}); using built-in fallback list")
            hospitals = []
    if not hospitals:
        source = "seed"
        clat, clon = cfg.AREA_CENTER
        hospitals = [{"name": nm, "lat": la, "lon": lo, "osm_id": None} for nm, la, lo in FALLBACK_DELHI_HOSPITALS
                     if haversine(clat, clon, la, lo) < cfg.AREA_SIZE_KM * 800]
    for h in hospitals:
        fac, beds = simulate_facilities(h["name"], h.get("osm_id") or h["name"], cfg.RANDOM_SEED)
        session.add(Hospital(name=h["name"], lat=h["lat"], lon=h["lon"], osm_id=h.get("osm_id"),
                             address=h.get("address"), phone=h.get("phone"), facilities=fac,
                             beds_available=beds, source=h.get("source", source)))
    log(f"  {len(hospitals)} hospitals stored (facilities and beds simulated)")
    return len(hospitals)


def seed_ambulances(session, net, cfg, log=print) -> int:
    """Simulated fleet: ~60 % parked at emergency hospitals, the rest at standby points on main roads."""
    if session.scalar(select(func.count(Ambulance.id))):
        return 0
    rng = random.Random(cfg.RANDOM_SEED)
    hospitals = [h for h in session.scalars(select(Hospital)) if "emergency" in (h.facilities or [])]
    n_total = cfg.SEED_AMBULANCES
    n_hosp = min(len(hospitals), int(n_total * 0.6))
    spots = []
    for h in rng.sample(hospitals, n_hosp):
        spots.append((h.lat, h.lon, h.name))
    major = [e for e in range(net.m) if net.e_major[e]] or list(range(net.m))
    while len(spots) < n_total and major:
        eid = rng.choice(major)
        u = net.e_u[eid]
        spots.append((net.lat[u], net.lon[u], f"Standby point, {net.e_name[eid]}"))
    for i, (lat, lon, base) in enumerate(spots, 1):
        roll = rng.random()
        status = "available" if roll < 0.85 else ("busy" if roll < 0.95 else "maintenance")
        session.add(Ambulance(code=f"AMB-{i:03d}", kind="ALS" if rng.random() < 0.4 else "BLS",
                              lat=round(lat, 6), lon=round(lon, 6), base_name=base[:200], status=status))
    log(f"  {len(spots)} simulated ambulances created")
    return len(spots)
