"""Download road data + hospitals from OpenStreetMap (Overpass API).

This does the same download OSMnx does (OSMnx also uses the Overpass API),
but in plain Python, so it is light enough to run inside a Vercel Function.

Result ("OSM data"), small enough to cache in the database:
    {"nodes": {node_id: [lat, lon]},
     "ways":  [[way_id, {tags}, [node ids]]],
     "pois":  [{"kind": "hospital", "name", "lat", "lon", "osm_id", "address", "phone"}]}
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from pathlib import Path

USER_AGENT = "AmbulanceRouteOptimizer/3.0 (student ADA project)"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# Roads an ambulance can drive on (same idea as OSMnx network_type="drive")
DRIVABLE = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "living_street", "road", "service",
}
EXCLUDED_SERVICE = {"parking_aisle", "parking", "driveway", "drive-through", "private", "emergency_access"}
KEEP_TAGS = ("highway", "oneway", "junction", "maxspeed", "name", "name:en", "ref", "service", "access")


def is_drivable(tags: dict) -> bool:
    hw = tags.get("highway")
    if hw not in DRIVABLE:
        return False
    if tags.get("access") in ("no", "private") or tags.get("motor_vehicle") in ("no", "private"):
        return False
    if tags.get("area") == "yes":
        return False
    if hw == "service" and tags.get("service") in EXCLUDED_SERVICE:
        return False
    return True


def poi_kind(tags: dict) -> str | None:
    if tags.get("amenity") == "hospital" or tags.get("healthcare") == "hospital":
        return "hospital"
    if tags.get("emergency") == "ambulance_station" or tags.get("amenity") == "ambulance_station":
        return "ambulance_station"
    return None


def _poi(tags, kind, lat, lon, osm_id):
    name = tags.get("name:en") or tags.get("name")
    if not name:
        return None
    addr = ", ".join(tags[k] for k in ("addr:housenumber", "addr:street", "addr:suburb", "addr:city") if tags.get(k))
    return {"kind": kind, "name": name.strip()[:200], "lat": lat, "lon": lon, "osm_id": osm_id,
            "address": addr or None, "phone": tags.get("phone")}


def overpass_query(bbox) -> str:
    s, w, n, e = bbox
    b = f"{s:.6f},{w:.6f},{n:.6f},{e:.6f}"
    hw = "|".join(sorted(DRIVABLE))
    return f"""
[out:json][timeout:90];
way["highway"~"^({hw})$"]({b})->.roads;
.roads out body;
node(w.roads);
out skel qt;
(
  nwr["amenity"="hospital"]({b});
  nwr["healthcare"="hospital"]({b});
  nwr["emergency"="ambulance_station"]({b});
);
out center;
"""


def parse_overpass(data: dict) -> dict:
    nodes, ways, pois, seen = {}, [], [], set()
    for el in data.get("elements", []):
        t, tags = el["type"], el.get("tags", {})
        kind = poi_kind(tags)
        if kind:
            lat = el.get("lat", el.get("center", {}).get("lat"))
            lon = el.get("lon", el.get("center", {}).get("lon"))
            oid = f"{t}/{el['id']}"
            if lat is not None and oid not in seen:
                seen.add(oid)
                p = _poi(tags, kind, lat, lon, oid)
                if p:
                    pois.append(p)
        if t == "node" and "lat" in el and not kind:
            nodes[el["id"]] = [el["lat"], el["lon"]]
        elif t == "way" and "nodes" in el and is_drivable(tags):
            ways.append([el["id"], {k: v for k, v in tags.items() if k in KEEP_TAGS}, el["nodes"]])
    return {"nodes": nodes, "ways": ways, "pois": pois}


def download(bbox, log=print, timeout: int = 100) -> dict:
    """Roads + hospitals inside bbox = (south, west, north, east)."""
    import requests

    query = overpass_query(bbox)
    last = None
    for attempt in range(2):
        for url in OVERPASS_URLS:
            try:
                log(f"Downloading roads and hospitals from OpenStreetMap ({url.split('/')[2]}) ...")
                t0 = time.time()
                r = requests.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=timeout)
                if r.status_code in (429, 503, 504):
                    raise RuntimeError(f"server busy (HTTP {r.status_code})")
                r.raise_for_status()
                osm = parse_overpass(r.json())
                if not osm["ways"]:
                    raise RuntimeError("no roads returned")
                log(f"  {len(osm['ways']):,} roads, {len(osm['nodes']):,} points, "
                    f"{sum(p['kind'] == 'hospital' for p in osm['pois'])} hospitals ({time.time() - t0:.0f}s)")
                return osm
            except Exception as e:  # try the next mirror
                last = e
                log(f"  failed: {e}")
        time.sleep(3)
    raise RuntimeError(f"Could not download OpenStreetMap data: {last}")


def load_osm_xml(path) -> dict:
    """Same result from a .osm file (exported from openstreetmap.org) - offline use and tests."""
    root = ET.parse(Path(path)).getroot()
    nodes, node_tags, ways, pois = {}, {}, [], []
    for el in root.iter("node"):
        nid = int(el.get("id"))
        nodes[nid] = [float(el.get("lat")), float(el.get("lon"))]
        tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
        if tags:
            node_tags[nid] = tags
    for nid, tags in node_tags.items():
        kind = poi_kind(tags)
        if kind:
            p = _poi(tags, kind, *nodes[nid], f"node/{nid}")
            if p:
                pois.append(p)
    for el in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
        refs = [int(nd.get("ref")) for nd in el.iter("nd")]
        kind = poi_kind(tags)
        if kind:
            pts = [nodes[r] for r in refs if r in nodes]
            if pts:
                p = _poi(tags, kind, sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts),
                         f"way/{el.get('id')}")
                if p:
                    pois.append(p)
        if is_drivable(tags):
            ways.append([int(el.get("id")), {k: v for k, v in tags.items() if k in KEEP_TAGS},
                         [r for r in refs if r in nodes]])
    return {"nodes": nodes, "ways": ways, "pois": pois}
