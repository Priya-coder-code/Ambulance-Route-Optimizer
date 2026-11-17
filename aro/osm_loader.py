"""Load real road networks from OpenStreetMap.

Online (default):
    fetch_area(center=(28.6315, 77.2167), size_km=50)
        Downloads every drivable road + all hospitals / ambulance stations in a
        size_km x size_km square from the OpenStreetMap Overpass API.
        Big areas are split into tiles (default 10 km); every tile is cached
        in data/cache/tiles/, so an interrupted download resumes where it stopped.

    fetch_area(place="Koramangala, Bengaluru", size_km=6)
        Same, but the centre is looked up with OpenStreetMap Nominatim.

Offline:
    load_osm_xml("map.osm")  -  a file exported from openstreetmap.org (Export button).

Map data (c) OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "AmbulanceRouteOptimizer/1.0 (student ADA project)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# Roads an ambulance can drive on.
MAJOR_ROADS = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link",
}
DRIVABLE = MAJOR_ROADS | {"unclassified", "residential", "living_street", "road"}
DRIVABLE_WITH_SERVICE = DRIVABLE | {"service"}
EXCLUDED_SERVICE = {"parking_aisle", "drive-through", "driveway"}

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
KM_PER_DEG_LAT = 111.32


@dataclass
class OSMData:
    name: str
    bounds: tuple[float, float, float, float]  # south, west, north, east
    nodes: dict[int, tuple[float, float]] = field(default_factory=dict)  # id -> (lat, lon)
    ways: list[tuple[dict, list[int]]] = field(default_factory=list)      # (tags, node ids)
    pois: list[dict] = field(default_factory=list)  # hospitals / ambulance stations


def is_drivable(tags: dict, road_types=DRIVABLE_WITH_SERVICE) -> bool:
    hw = tags.get("highway")
    if hw not in road_types:
        return False
    if tags.get("access") in ("no", "private") or tags.get("motor_vehicle") in ("no", "private"):
        return False
    if hw == "service" and tags.get("service") in EXCLUDED_SERVICE:
        return False
    if tags.get("area") == "yes":
        return False
    return True


def poi_kind(tags: dict) -> str | None:
    if tags.get("amenity") == "hospital" or tags.get("healthcare") == "hospital":
        return "hospital"
    if tags.get("emergency") == "ambulance_station" or tags.get("amenity") == "ambulance_station":
        return "ambulance_station"
    return None


def _poi_name(tags: dict, kind: str) -> str:
    return tags.get("name:en") or tags.get("name") or ("Hospital" if kind == "hospital" else "Ambulance station")


KEEP_TAGS = ("highway", "oneway", "junction", "maxspeed", "name", "name:en", "service", "access")


# ----------------------------------------------------------------------------
# Area helpers
# ----------------------------------------------------------------------------

def square_bbox(lat: float, lon: float, size_km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a size_km x size_km square centred on (lat, lon)."""
    half = size_km / 2
    dlat = half / KM_PER_DEG_LAT
    dlon = half / (KM_PER_DEG_LAT * max(0.01, math.cos(math.radians(lat))))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def split_bbox(bbox, tile_km: float):
    s, w, n, e = bbox
    lat_c = (s + n) / 2
    h_km = (n - s) * KM_PER_DEG_LAT
    w_km = (e - w) * KM_PER_DEG_LAT * math.cos(math.radians(lat_c))
    rows = max(1, math.ceil(h_km / tile_km - 1e-9))
    cols = max(1, math.ceil(w_km / tile_km - 1e-9))
    dlat, dlon = (n - s) / rows, (e - w) / cols
    return [(round(s + r * dlat, 6), round(w + c * dlon, 6), round(s + (r + 1) * dlat, 6), round(w + (c + 1) * dlon, 6))
            for r in range(rows) for c in range(cols)]


def geocode(place: str) -> tuple[float, float, str]:
    """Place name -> (lat, lon, display_name) via OpenStreetMap Nominatim."""
    import requests

    r = requests.get(NOMINATIM_URL, params={"q": place, "format": "json", "limit": 1},
                     headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    hits = r.json()
    if not hits:
        raise ValueError(f"Could not find '{place}' on OpenStreetMap")
    h = hits[0]
    return float(h["lat"]), float(h["lon"]), h.get("display_name", place)


# ----------------------------------------------------------------------------
# Overpass API
# ----------------------------------------------------------------------------

def overpass_query(bbox, road_types=DRIVABLE_WITH_SERVICE) -> str:
    s, w, n, e = bbox
    b = f"{s:.6f},{w:.6f},{n:.6f},{e:.6f}"
    hw = "|".join(sorted(road_types))
    return f"""
[out:json][timeout:300][maxsize:1073741824];
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


def parse_overpass_json(data: dict, road_types=DRIVABLE_WITH_SERVICE) -> dict:
    """Overpass JSON -> compact tile dict {nodes, ways, pois}."""
    nodes, ways, pois = {}, [], []
    for el in data.get("elements", []):
        t = el["type"]
        tags = el.get("tags", {})
        kind = poi_kind(tags)
        if kind:
            if t == "node":
                lat, lon = el["lat"], el["lon"]
            elif "center" in el:
                lat, lon = el["center"]["lat"], el["center"]["lon"]
            else:
                lat = lon = None
            if lat is not None:
                pois.append({"kind": kind, "name": _poi_name(tags, kind), "lat": lat, "lon": lon,
                             "osm_id": f"{t}/{el['id']}"})
        if t == "node" and "lat" in el:
            nodes[el["id"]] = (el["lat"], el["lon"])
        elif t == "way" and "nodes" in el and is_drivable(tags, road_types):
            ways.append([el["id"], {k: v for k, v in tags.items() if k in KEEP_TAGS}, el["nodes"]])
    return {"nodes": nodes, "ways": ways, "pois": pois}


def _download_tile(bbox, road_types, log) -> dict:
    import requests

    query = overpass_query(bbox, road_types)
    last_err = None
    for attempt in range(3):
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=600)
                if r.status_code in (429, 503, 504):
                    raise RuntimeError(f"{url.split('/')[2]} busy (HTTP {r.status_code})")
                r.raise_for_status()
                return parse_overpass_json(r.json(), road_types)
            except Exception as e:
                last_err = e
                log(f"    {e} - retrying")
        time.sleep(10 * (attempt + 1))  # Overpass rate limit: back off
    raise RuntimeError(f"Could not download OpenStreetMap data: {last_err}")


def fetch_area(center: tuple[float, float] | None = None, size_km: float = 50, place: str | None = None,
               tile_km: float = 10, include_service: bool = False, name: str | None = None,
               cache_dir: Path | str = DEFAULT_CACHE_DIR, refresh: bool = False, log=print) -> OSMData:
    """Download (or load from the tile cache) roads + hospitals in a square area."""
    if center is None:
        if not place:
            raise ValueError("give center=(lat, lon) or place='...'")
        log(f"Looking up '{place}' with OpenStreetMap Nominatim ...")
        lat, lon, display = geocode(place)
        log(f"  found {display[:70]} at {lat:.5f}, {lon:.5f}")
        center = (lat, lon)
    road_types = DRIVABLE_WITH_SERVICE if include_service else DRIVABLE
    bbox = square_bbox(center[0], center[1], size_km)
    tiles = split_bbox(bbox, tile_km)
    tile_dir = Path(cache_dir) / "tiles"
    tile_dir.mkdir(parents=True, exist_ok=True)
    osm = OSMData(name=name or place or f"{size_km:g} x {size_km:g} km around {center[0]:.4f}, {center[1]:.4f}",
                  bounds=bbox)
    log(f"Area {size_km:g} x {size_km:g} km -> {len(tiles)} tile(s) of ~{tile_km:g} km "
        f"from the OpenStreetMap Overpass API")
    seen_ways, seen_pois = set(), set()
    for i, tb in enumerate(tiles, 1):
        key = hashlib.sha1(f"{tb}|{sorted(road_types)}".encode()).hexdigest()[:16]
        f = tile_dir / f"tile_{key}.json"
        if f.exists() and not refresh:
            tile = json.loads(f.read_text(encoding="utf-8"))
            src = "cache"
        else:
            log(f"  tile {i}/{len(tiles)}: downloading ...")
            t0 = time.time()
            tile = _download_tile(tb, road_types, log)
            f.write_text(json.dumps(tile), encoding="utf-8")
            src = f"{time.time() - t0:.0f}s"
        for nid, ll in tile["nodes"].items():
            osm.nodes[int(nid)] = tuple(ll)
        for wid, tags, refs in tile["ways"]:
            if wid not in seen_ways:
                seen_ways.add(wid)
                osm.ways.append((tags, refs))
        for p in tile["pois"]:
            if p["osm_id"] not in seen_pois:
                seen_pois.add(p["osm_id"])
                osm.pois.append(p)
        log(f"  tile {i}/{len(tiles)} ok ({src}): total {len(osm.ways):,} roads, {len(osm.nodes):,} road points, "
            f"{sum(p['kind'] == 'hospital' for p in osm.pois)} hospitals")
    if not osm.ways:
        raise RuntimeError("No roads found in this area")
    return osm


def area_cache_key(center, size_km, include_service, ignore_oneway) -> str:
    return hashlib.sha1(f"{center[0]:.5f},{center[1]:.5f}|{size_km}|{include_service}|{ignore_oneway}|v2"
                        .encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------
# Offline: .osm XML files
# ----------------------------------------------------------------------------

def load_osm_xml(path: str | Path, name: str | None = None, include_service: bool = True) -> OSMData:
    """Parse a .osm XML file (e.g. exported from openstreetmap.org)."""
    road_types = DRIVABLE_WITH_SERVICE if include_service else DRIVABLE
    path = Path(path)
    if path.suffix == ".bz2":
        import bz2
        src = bz2.open(path, "rb")
    else:
        src = open(path, "rb")
    with src:
        root = ET.parse(src).getroot()

    nodes: dict[int, tuple[float, float]] = {}
    node_tags: dict[int, dict] = {}
    for el in root.iter("node"):
        nid = int(el.get("id"))
        nodes[nid] = (float(el.get("lat")), float(el.get("lon")))
        tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
        if tags:
            node_tags[nid] = tags

    b = root.find("bounds")
    if b is not None:
        bounds = (float(b.get("minlat")), float(b.get("minlon")), float(b.get("maxlat")), float(b.get("maxlon")))
    else:
        lats = [p[0] for p in nodes.values()]
        lons = [p[1] for p in nodes.values()]
        bounds = (min(lats), min(lons), max(lats), max(lons))

    osm = OSMData(name=name or path.stem, bounds=bounds, nodes=nodes)
    for nid, tags in node_tags.items():
        kind = poi_kind(tags)
        if kind:
            lat, lon = nodes[nid]
            osm.pois.append({"kind": kind, "name": _poi_name(tags, kind), "lat": lat, "lon": lon,
                             "osm_id": f"node/{nid}"})
    for el in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
        refs = [int(nd.get("ref")) for nd in el.iter("nd")]
        kind = poi_kind(tags)
        if kind:
            pts = [nodes[r] for r in refs if r in nodes]
            if pts:
                osm.pois.append({"kind": kind, "name": _poi_name(tags, kind),
                                 "lat": sum(p[0] for p in pts) / len(pts),
                                 "lon": sum(p[1] for p in pts) / len(pts),
                                 "osm_id": f"way/{el.get('id')}"})
        if is_drivable(tags, road_types):
            osm.ways.append((tags, [r for r in refs if r in nodes]))
    return osm


def load_custom_pois(path: str | Path) -> list[dict]:
    """Extra hospitals / ambulance bases from a JSON list:
    [{"kind": "hospital", "name": "...", "lat": .., "lon": ..}, ...]"""
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    for it in items:
        it.setdefault("kind", "hospital")
        it.setdefault("name", it["kind"].replace("_", " ").title())
        it.setdefault("osm_id", f"custom/{it['lat']},{it['lon']}")
    return items
