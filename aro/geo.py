"""Geographic helpers: great-circle distance, area boxes and a grid spatial index."""

from __future__ import annotations

import math
from collections import defaultdict

EARTH_RADIUS_M = 6_371_008.8
KM_PER_DEG_LAT = 111.32


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line ('as the crow flies') distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def square_bbox(lat: float, lon: float, size_km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a size_km x size_km square centred on (lat, lon)."""
    half = size_km / 2
    dlat = half / KM_PER_DEG_LAT
    dlon = half / (KM_PER_DEG_LAT * max(0.01, math.cos(math.radians(lat))))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


class GridIndex:
    """Uniform-grid spatial index: exact nearest-neighbour and radius queries."""

    def __init__(self, lats, lons, cell_deg: float = 0.002):
        self.cell = cell_deg
        self.lats, self.lons = lats, lons
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i in range(len(lats)):
            self.cells[self._key(lats[i], lons[i])].append(i)
        mean_lat = sum(lats) / len(lats) if len(lats) else 0.0
        self.cell_m = cell_deg * KM_PER_DEG_LAT * 1000 * min(1.0, math.cos(math.radians(mean_lat)))
        ks = list(self.cells)
        self.imin = min((k[0] for k in ks), default=0)
        self.imax = max((k[0] for k in ks), default=0)
        self.jmin = min((k[1] for k in ks), default=0)
        self.jmax = max((k[1] for k in ks), default=0)

    def _key(self, lat, lon):
        return (int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell)))

    def nearest(self, lat: float, lon: float) -> tuple[int, float]:
        """(index, distance_m) of the closest point."""
        if not self.cells:
            raise ValueError("empty index")
        ci, cj = self._key(lat, lon)
        last = max(abs(ci - self.imin), abs(ci - self.imax), abs(cj - self.jmin), abs(cj - self.jmax))
        if last > 400:  # far outside the area: brute force
            return min(((i, haversine(lat, lon, self.lats[i], self.lons[i])) for i in range(len(self.lats))),
                       key=lambda t: t[1])
        best, best_d = -1, math.inf
        for ring in range(last + 1):
            if best >= 0 and (ring - 1) * self.cell_m > best_d:
                break
            for i in range(ci - ring, ci + ring + 1):
                step = 1 if abs(i - ci) == ring else max(1, 2 * ring)
                for j in range(cj - ring, cj + ring + 1, step):
                    for idx in self.cells.get((i, j), ()):
                        d = haversine(lat, lon, self.lats[idx], self.lons[idx])
                        if d < best_d:
                            best, best_d = idx, d
        return best, best_d

    def within(self, lat: float, lon: float, radius_m: float) -> list[int]:
        ci, cj = self._key(lat, lon)
        rings = int(radius_m / max(1.0, self.cell_m)) + 1
        out = []
        for i in range(ci - rings, ci + rings + 1):
            for j in range(cj - rings, cj + rings + 1):
                for idx in self.cells.get((i, j), ()):
                    if haversine(lat, lon, self.lats[idx], self.lons[idx]) <= radius_m:
                        out.append(idx)
        return out
