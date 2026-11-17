"""Dijkstra's shortest-path algorithm (written from scratch, binary heap).

    Time  O((V + E) log V)      Space  O(V)

Options used by the planner:
  targets     stop early once the target (or `stop_after` of several targets) is settled
  cutoff      stop when the smallest tentative time exceeds this value (pruning:
              no route longer than the best one found so far can win)
  record      keep the settle order, to visualise the search on the map
  stop_condition  custom early exit, e.g. "K suitable hospitals reached"
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass, field


@dataclass
class DijkstraResult:
    source: int
    dist: dict[int, float]                     # final shortest time for every settled node
    parent: dict[int, tuple[int, int]]         # node -> (previous node, edge id)
    reached: list[int]                         # targets in the order they were settled (fastest first)
    settled: int = 0                           # nodes removed from the heap (= "visited")
    relaxed: int = 0                           # edges examined
    pushes: int = 0                            # heap insertions
    max_heap: int = 0
    runtime_ms: float = 0.0
    stopped_by_cutoff: bool = False
    explored: list[int] = field(default_factory=list)

    def path_to(self, target: int) -> tuple[list[int], list[int]]:
        """Node list and edge list from the source to `target` (follow parent pointers back)."""
        if target not in self.dist:
            return [], []
        nodes, edges = [target], []
        while nodes[-1] != self.source:
            prev, eid = self.parent[nodes[-1]]
            nodes.append(prev)
            edges.append(eid)
        nodes.reverse()
        edges.reverse()
        return nodes, edges


def dijkstra(adj, weights, source: int, targets=None, stop_after: int | None = 1,
             cutoff: float = math.inf, record: bool = False, stop_condition=None) -> DijkstraResult:
    """adj[u] = [(v, edge_id), ...]; weights[edge_id] = travel time (inf = closed road).

    stop_condition(node) -> bool, if given, is called each time a target is settled
    and replaces `stop_after` (used to stop after the K fastest *suitable* hospitals)."""
    t0 = time.perf_counter()
    goal = set(targets) if targets is not None else None
    want = len(goal) if (goal is not None and stop_after is None) else (stop_after or 0)

    dist: dict[int, float] = {source: 0.0}
    parent: dict[int, tuple[int, int]] = {}
    done: set[int] = set()
    heap = [(0.0, source)]
    res = DijkstraResult(source, {}, parent, [], pushes=1, max_heap=1)
    explored = res.explored

    while heap:
        d, u = heapq.heappop(heap)
        if u in done:               # outdated heap entry (lazy deletion)
            continue
        if d > cutoff:              # every remaining node is even further away
            res.stopped_by_cutoff = True
            break
        done.add(u)
        if record:
            explored.append(u)
        if goal is not None and u in goal:
            res.reached.append(u)
            if stop_condition is not None:
                if stop_condition(u):
                    break
            elif len(res.reached) >= want:
                break
        for v, eid in adj[u]:
            res.relaxed += 1
            nd = d + weights[eid]
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                parent[v] = (u, eid)
                heapq.heappush(heap, (nd, v))
                res.pushes += 1
        if len(heap) > res.max_heap:
            res.max_heap = len(heap)

    res.dist = {n: dist[n] for n in done}
    res.settled = len(done)
    res.runtime_ms = (time.perf_counter() - t0) * 1000
    return res


def bellman_ford(n: int, edges, weights, source: int) -> list[float]:
    """O(V * E) reference algorithm, only used by the tests to check Dijkstra."""
    dist = [math.inf] * n
    dist[source] = 0.0
    for _ in range(n - 1):
        changed = False
        for eid, (u, v) in enumerate(edges):
            if dist[u] + weights[eid] < dist[v]:
                dist[v] = dist[u] + weights[eid]
                changed = True
        if not changed:
            break
    return dist
