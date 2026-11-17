"""Address <-> latitude/longitude with the OpenStreetMap Nominatim API.

Nominatim's usage policy: max 1 request per second, a real User-Agent, and no
search-as-you-type. This module enforces the rate limit and caches results.
"""

from __future__ import annotations

import threading
import time
from functools import lru_cache

import requests

from .geo import square_bbox

_lock = threading.Lock()
_last = [0.0]


def _get(cfg, path: str, params: dict):
    with _lock:
        wait = 1.05 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
    r = requests.get(f"{cfg.NOMINATIM_URL}/{path}", params=params,
                     headers={"User-Agent": cfg.USER_AGENT, "Accept-Language": "en"}, timeout=20)
    r.raise_for_status()
    return r.json()


def search(cfg, query: str, limit: int = 6) -> list[dict]:
    return list(_search_cached(cfg, query.strip(), limit))


@lru_cache(maxsize=512)
def _search_cached(cfg, query: str, limit: int):
    s, w, n, e = square_bbox(*cfg.AREA_CENTER, cfg.AREA_SIZE_KM)
    data = _get(cfg, "search", {"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 0,
                                "viewbox": f"{w},{n},{e},{s}", "bounded": 1})
    return tuple({"label": d.get("display_name", ""), "lat": float(d["lat"]), "lon": float(d["lon"]),
                  "type": d.get("type", "")} for d in data)


@lru_cache(maxsize=2048)
def _reverse_cached(cfg, lat: float, lon: float) -> str:
    data = _get(cfg, "reverse", {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 17})
    return data.get("display_name", "")


def reverse(cfg, lat: float, lon: float) -> str:
    return _reverse_cached(cfg, round(lat, 5), round(lon, 5))
