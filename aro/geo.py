"""Geographic helpers: great-circle distance and a grid spatial index for snapping
clicked map points / hospitals to the nearest road-network node."""

from __future__ import annotations

import math
from collections import defaultdict

EARTH_RADIUS_M = 6_371_008.8
METERS_PER_DEG_LAT = 111_320.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two (lat, lon) points.

    This is the 'straight line' (as the crow flies) distance used by the A*
    heuristic. No road between two points can be shorter than this, which is
    what makes the heuristic admissible."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bbox_around(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) box of +/- radius_m around a point."""
    dlat = radius_m / METERS_PER_DEG_LAT
    dlon = radius_m / (METERS_PER_DEG_LAT * max(0.01, math.cos(math.radians(lat))))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


class GridIndex:
    """Uniform-grid spatial index. Exact nearest-neighbour search by scanning
    rings of cells outward until no closer point can exist."""

    def __init__(self, lats: list[float], lons: list[float], cell_deg: float = 0.002):
        self.cell = cell_deg
        self.lats = lats
        self.lons = lons
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, (la, lo) in enumerate(zip(lats, lons)):
            self.cells[self._key(la, lo)].append(i)
        mean_lat = sum(lats) / len(lats) if lats else 0.0
        # smallest side of a cell in metres (longitude side shrinks with latitude)
        self.cell_m = cell_deg * METERS_PER_DEG_LAT * min(1.0, math.cos(math.radians(mean_lat)))
        ks = list(self.cells)
        self.imin = min((k[0] for k in ks), default=0)
        self.imax = max((k[0] for k in ks), default=0)
        self.jmin = min((k[1] for k in ks), default=0)
        self.jmax = max((k[1] for k in ks), default=0)

    def _key(self, lat: float, lon: float) -> tuple[int, int]:
        return (int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell)))

    def _brute(self, lat: float, lon: float) -> tuple[int, float]:
        best, best_d = -1, math.inf
        for idx in range(len(self.lats)):
            d = haversine(lat, lon, self.lats[idx], self.lons[idx])
            if d < best_d:
                best, best_d = idx, d
        return best, best_d

    def within(self, lat: float, lon: float, radius_m: float) -> list[int]:
        """Indices of all points within radius_m of (lat, lon)."""
        ci, cj = self._key(lat, lon)
        rings = int(radius_m / max(1.0, self.cell_m)) + 1
        out = []
        for i in range(ci - rings, ci + rings + 1):
            for j in range(cj - rings, cj + rings + 1):
                for idx in self.cells.get((i, j), ()):
                    if haversine(lat, lon, self.lats[idx], self.lons[idx]) <= radius_m:
                        out.append(idx)
        return out

    def nearest(self, lat: float, lon: float) -> tuple[int, float]:
        """Return (index, distance_m) of the closest indexed point."""
        if not self.cells:
            raise ValueError("empty index")
        ci, cj = self._key(lat, lon)
        last_ring = max(abs(ci - self.imin), abs(ci - self.imax),
                        abs(cj - self.jmin), abs(cj - self.jmax))
        if last_ring > 400:  # query far outside the map
            return self._brute(lat, lon)
        best, best_d = -1, math.inf
        for ring in range(last_ring + 1):
            # every point in this ring is at least (ring - 1) cell-widths away
            if best >= 0 and (ring - 1) * self.cell_m > best_d:
                break
            for i in range(ci - ring, ci + ring + 1):
                edge_row = abs(i - ci) == ring
                step = 1 if edge_row else 2 * ring  # only the ring's border cells
                for j in range(cj - ring, cj + ring + 1, max(1, step)):
                    for idx in self.cells.get((i, j), ()):
                        d = haversine(lat, lon, self.lats[idx], self.lons[idx])
                        if d < best_d:
                            best, best_d = idx, d
        return best, best_d
