"""Dijkstra's algorithm and A* search, written from scratch and instrumented.

Both use a binary min-heap (heapq) with lazy deletion:
  Dijkstra : O((V + E) log V) time, O(V) space
  A*       : same worst case; with an admissible + consistent heuristic it
             settles every node at most once and usually settles far fewer.

Both support
  * several targets at once  -> "nearest hospital" in a single search
  * searching the reversed graph -> "which ambulance can reach me fastest"
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass, field
from typing import Iterable

from .graph import PROJECTION_SAFETY


@dataclass
class SearchResult:
    algorithm: str
    source: int
    target: int | None          # the target actually reached (closest one)
    path: list[int]             # node ids from source to target (empty if unreachable)
    path_edges: list[int]       # edge ids along the path (len(path) - 1)
    cost: float                 # total weight (seconds or metres)
    nodes_explored: int         # nodes settled (popped from the heap for the first time)
    edges_relaxed: int          # edge relaxations attempted
    heap_pushes: int
    max_frontier: int           # largest heap size seen (memory use)
    runtime_ms: float
    explored_order: list[int] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.target is not None


def _reconstruct(parent: dict[int, tuple[int, int]], source: int, target: int):
    """Follow parent pointers back from the target. Returns (nodes, edges)."""
    nodes, edges = [target], []
    while nodes[-1] != source:
        prev, eid = parent[nodes[-1]]
        nodes.append(prev)
        edges.append(eid)
    nodes.reverse()
    edges.reverse()
    return nodes, edges


def dijkstra(graph, source: int, targets: int | Iterable[int] | None, metric: str = "time",
             reverse: bool = False, record: bool = True) -> SearchResult:
    """Dijkstra's shortest path from `source` to the closest of `targets`.

    targets=None explores the whole graph (single-source shortest paths).
    reverse=True walks edges backwards (finds who can reach `source` fastest)."""
    t0 = time.perf_counter()
    goal = _as_set(targets)
    w = graph.weights(metric)
    adj = graph.radj if reverse else graph.adj

    dist = {source: 0.0}
    parent: dict[int, tuple[int, int]] = {}
    settled: set[int] = set()
    heap = [(0.0, source)]
    pushes, relaxed, max_frontier = 1, 0, 1
    order: list[int] = []
    reached = None

    while heap:
        d, u = heapq.heappop(heap)
        if u in settled:            # stale entry (lazy deletion)
            continue
        settled.add(u)
        if record:
            order.append(u)
        if goal is not None and u in goal:
            reached = u
            break
        for v, eid in adj[u]:
            relaxed += 1
            nd = d + w[eid]
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                parent[v] = (u, eid)
                heapq.heappush(heap, (nd, v))
                pushes += 1
        if len(heap) > max_frontier:
            max_frontier = len(heap)

    path, pedges = _reconstruct(parent, source, reached) if reached is not None else ([], [])
    return SearchResult("Dijkstra", source, reached, path, pedges,
                        dist.get(reached, math.inf) if reached is not None else math.inf,
                        len(settled), relaxed, pushes, max_frontier,
                        (time.perf_counter() - t0) * 1000, order)


def astar(graph, source: int, targets: int | Iterable[int], metric: str = "time",
          reverse: bool = False, record: bool = True, heuristic_weight: float = 1.0) -> SearchResult:
    """A* search: like Dijkstra, but the heap is ordered by f(n) = g(n) + h(n).

    g(n) = best known cost from the source to n
    h(n) = straight-line distance from n to the nearest target, converted to
           the metric (divided by the top speed for travel time). It never
           overestimates, so the route found is still optimal.

    heuristic_weight > 1 gives "weighted A*": faster, but no longer guaranteed optimal
    (kept only for experiments in the report)."""
    t0 = time.perf_counter()
    goal = _as_set(targets)
    if not goal:
        raise ValueError("A* needs at least one target")
    w = graph.weights(metric)
    adj = graph.radj if reverse else graph.adj
    xs, ys = graph.projected_xy()
    scale = graph.heuristic_scale(metric) * heuristic_weight * PROJECTION_SAFETY
    goal_pts = [(xs[t], ys[t]) for t in goal]
    hypot = math.hypot

    if len(goal_pts) == 1:
        gx, gy = goal_pts[0]

        def h(n: int) -> float:
            return scale * hypot(xs[n] - gx, ys[n] - gy)
    else:
        def h(n: int) -> float:  # nearest of several targets
            x, y = xs[n], ys[n]
            return scale * min(hypot(x - tx, y - ty) for tx, ty in goal_pts)

    g = {source: 0.0}
    parent: dict[int, tuple[int, int]] = {}
    closed: set[int] = set()
    heap = [(h(source), 0.0, source)]
    pushes, relaxed, max_frontier = 1, 0, 1
    order: list[int] = []
    reached = None

    while heap:
        _, gu, u = heapq.heappop(heap)
        if u in closed:
            continue
        closed.add(u)
        if record:
            order.append(u)
        if u in goal:
            reached = u
            break
        for v, eid in adj[u]:
            relaxed += 1
            ng = gu + w[eid]
            if ng < g.get(v, math.inf):
                g[v] = ng
                parent[v] = (u, eid)
                if v in closed:          # only possible with an inconsistent heuristic
                    closed.discard(v)
                heapq.heappush(heap, (ng + h(v), ng, v))
                pushes += 1
        if len(heap) > max_frontier:
            max_frontier = len(heap)

    path, pedges = _reconstruct(parent, source, reached) if reached is not None else ([], [])
    name = "A*" if heuristic_weight == 1.0 else f"Weighted A* (w={heuristic_weight:g})"
    return SearchResult(name, source, reached, path, pedges,
                        g.get(reached, math.inf) if reached is not None else math.inf,
                        len(closed), relaxed, pushes, max_frontier,
                        (time.perf_counter() - t0) * 1000, order)


def _as_set(targets) -> set[int] | None:
    if targets is None:
        return None
    if isinstance(targets, int):
        return {targets}
    return set(targets)


ALGORITHMS = {"dijkstra": dijkstra, "astar": astar}


def run(name: str, graph, source: int, targets, metric: str = "time", reverse: bool = False,
        record: bool = True) -> SearchResult:
    return ALGORITHMS[name](graph, source, targets, metric=metric, reverse=reverse, record=record)


# ---------------------------------------------------------------------------
# Reference implementation used only by the unit tests.
# ---------------------------------------------------------------------------
def bellman_ford(graph, source: int, metric: str = "time") -> list[float]:
    """O(V * E) brute-force shortest paths, to check Dijkstra and A* against."""
    w = graph.weights(metric)
    dist = [math.inf] * graph.n
    dist[source] = 0.0
    for _ in range(graph.n - 1):
        changed = False
        for eid, e in enumerate(graph.edges):
            if dist[e.u] + w[eid] < dist[e.v]:
                dist[e.v] = dist[e.u] + w[eid]
                changed = True
        if not changed:
            break
    return dist
