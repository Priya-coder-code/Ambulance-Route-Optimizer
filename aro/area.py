"""Turn an area request into a ready-to-use (graph, dispatcher) pair.

The finished graph is pickled to data/cache/, so the 50 x 50 km Delhi map is
downloaded and built only once; later starts take a few seconds.
"""

from __future__ import annotations

import gc
import pickle
import time
from dataclasses import dataclass
from pathlib import Path

from .dispatcher import Dispatcher
from .graph import RoadGraph
from .osm_loader import DEFAULT_CACHE_DIR, area_cache_key, fetch_area, load_custom_pois, load_osm_xml

ROOT = Path(__file__).resolve().parent.parent

# Central Delhi: Connaught Place, 50 km x 50 km square
DELHI_CENTER = (28.6315, 77.2167)
DELHI_SIZE_KM = 50
DELHI_NAME = "Central Delhi (50 x 50 km around Connaught Place)"

# Names that mean "the default Delhi area" (avoids re-downloading for a slightly different centre)
PRESETS = {"connaught place, new delhi": DELHI_CENTER, "connaught place": DELHI_CENTER,
           "central delhi": DELHI_CENTER, "delhi": DELHI_CENTER, "new delhi": DELHI_CENTER}

SAMPLE_OSM = ROOT / "sample_data" / "sample_town.osm"
SAMPLE_POIS = ROOT / "sample_data" / "sample_town_hospitals.json"


@dataclass
class AreaSpec:
    center: tuple[float, float] | None = DELHI_CENTER
    size_km: float = DELHI_SIZE_KM
    place: str | None = None
    name: str | None = DELHI_NAME
    osm_file: str | None = None
    sample: bool = False
    pois_file: str | None = None
    include_service: bool = False
    ignore_oneway: bool = False
    refresh: bool = False
    tile_km: float = 10
    cache_dir: str | None = None


def load_area(spec: AreaSpec, log=print) -> tuple[RoadGraph, list[dict]]:
    """Returns (graph, pois). Uses the graph cache when possible."""
    result = _load_area(spec, log)
    # The graph is millions of long-lived objects. Without this, Python's cyclic
    # garbage collector keeps re-scanning them and random searches pause for seconds.
    gc.collect()
    gc.freeze()
    return result


def _load_area(spec: AreaSpec, log=print) -> tuple[RoadGraph, list[dict]]:
    t0 = time.time()
    extra = load_custom_pois(spec.pois_file) if spec.pois_file else []

    if spec.osm_file or spec.sample:
        path = SAMPLE_OSM if spec.sample else spec.osm_file
        log(f"Reading {Path(path).name} ...")
        osm = load_osm_xml(path, name="Offline sample (real OSM extract)" if spec.sample else Path(path).stem)
        if spec.sample and not spec.pois_file:
            extra = load_custom_pois(SAMPLE_POIS)
        g = RoadGraph.from_osm(osm, ignore_oneway=spec.ignore_oneway, log=log)
        return g, list(osm.pois) + extra

    center = spec.center
    if center is None and spec.place and spec.place.strip().lower() in PRESETS:
        center = PRESETS[spec.place.strip().lower()]
    if center is None:
        from .osm_loader import geocode
        log(f"Looking up '{spec.place}' with OpenStreetMap Nominatim ...")
        lat, lon, display = geocode(spec.place)
        center = (lat, lon)
        log(f"  found {display[:70]}")
    key = area_cache_key(center, spec.size_km, spec.include_service, spec.ignore_oneway)
    gfile = Path(spec.cache_dir or DEFAULT_CACHE_DIR) / f"graph_{key}.pkl"
    if gfile.exists() and not spec.refresh:
        log(f"Loading prepared graph from cache ({gfile.name}) ...")
        with open(gfile, "rb") as f:
            g, pois = pickle.load(f)
        g.finalize()
        log(f"  ready in {time.time() - t0:.1f}s")
        return g, pois + extra

    name = spec.name if spec.center is not None else f"{spec.place} ({spec.size_km:g} x {spec.size_km:g} km)"
    osm = fetch_area(center=center, size_km=spec.size_km, tile_km=spec.tile_km, name=name,
                     include_service=spec.include_service, refresh=spec.refresh, log=log,
                     cache_dir=spec.cache_dir or DEFAULT_CACHE_DIR)
    log("Building the road graph (one-time; may take a minute for a big city) ...")
    g = RoadGraph.from_osm(osm, ignore_oneway=spec.ignore_oneway, log=log)
    pois = list(osm.pois)
    del osm
    gfile.parent.mkdir(parents=True, exist_ok=True)
    with open(gfile, "wb") as f:
        pickle.dump((g, pois), f, protocol=pickle.HIGHEST_PROTOCOL)
    log(f"  graph cached -> {gfile.name} ({time.time() - t0:.0f}s total)")
    return g, pois + extra


def make_dispatcher(graph: RoadGraph, pois: list[dict]) -> Dispatcher:
    return Dispatcher.from_pois(graph, pois)
