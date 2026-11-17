"""Experiments for the report: Dijkstra vs A* on real and synthetic road networks.

Produces (in results/):
  real_map_queries.csv        one row per random query on the real map
  scaling.csv                 averages per synthetic city size
  nodes_vs_distance.png       nodes explored vs trip length (real map)
  scaling_nodes.png           nodes explored vs graph size
  scaling_runtime.png         runtime vs graph size
  speedup_by_metric.png       A* savings for "fastest time" vs "shortest distance"
"""

from __future__ import annotations

import csv
import math
import random
import statistics
from pathlib import Path

from .algorithms import astar, dijkstra
from .geo import haversine

COL_D = "#7c4dff"
COL_A = "#11865a"


def _pairs(graph, k, rng, min_m=0.0):
    out = []
    tries = 0
    while len(out) < k and tries < k * 50:
        tries += 1
        s, t = rng.randrange(graph.n), rng.randrange(graph.n)
        if s == t:
            continue
        if haversine(graph.lat[s], graph.lon[s], graph.lat[t], graph.lon[t]) >= min_m:
            out.append((s, t))
    return out


def compare(graph, pairs, metric="time"):
    rows = []
    for s, t in pairs:
        d = dijkstra(graph, s, t, metric, record=False)
        a = astar(graph, s, t, metric, record=False)
        if not (d.found and a.found):
            continue
        if abs(d.cost - a.cost) > 1e-6 * max(1, d.cost):
            raise AssertionError(f"A* not optimal for {s}->{t}: {a.cost} vs {d.cost}")
        rows.append({
            "metric": metric,
            "source": s, "target": t,
            "straight_m": round(haversine(graph.lat[s], graph.lon[s], graph.lat[t], graph.lon[t]), 1),
            "cost": round(d.cost, 3),
            "dijkstra_nodes": d.nodes_explored, "astar_nodes": a.nodes_explored,
            "dijkstra_relaxed": d.edges_relaxed, "astar_relaxed": a.edges_relaxed,
            "dijkstra_ms": round(d.runtime_ms, 3), "astar_ms": round(a.runtime_ms, 3),
        })
    return rows


def _save_csv(rows, path):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _style(ax, title, xlabel, ylabel):
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.25)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(frameon=False)


def run_real(graph, out_dir: Path, queries=200, seed=7, log=print):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rng = random.Random(seed)
    pairs = _pairs(graph, queries, rng)
    rows = compare(graph, pairs, "time") + compare(graph, pairs, "distance")
    _save_csv(rows, out_dir / "real_map_queries.csv")

    t_rows = [r for r in rows if r["metric"] == "time"]
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    xs = [r["straight_m"] / 1000 for r in t_rows]
    ax.scatter(xs, [r["dijkstra_nodes"] for r in t_rows], s=14, color=COL_D, alpha=0.7, label="Dijkstra")
    ax.scatter(xs, [r["astar_nodes"] for r in t_rows], s=14, color=COL_A, alpha=0.7, label="A*")
    _style(ax, f"Nodes explored vs trip length – {graph.name}", "straight-line distance source→target (km)",
           "nodes explored")
    fig.tight_layout()
    fig.savefig(out_dir / "nodes_vs_distance.png", dpi=150)
    plt.close(fig)

    summary = {}
    for metric in ("time", "distance"):
        mr = [r for r in rows if r["metric"] == metric]
        summary[metric] = {
            "queries": len(mr),
            "dijkstra_nodes": statistics.mean(r["dijkstra_nodes"] for r in mr),
            "astar_nodes": statistics.mean(r["astar_nodes"] for r in mr),
            "dijkstra_ms": statistics.mean(r["dijkstra_ms"] for r in mr),
            "astar_ms": statistics.mean(r["astar_ms"] for r in mr),
        }
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    labels = ["Fastest time", "Shortest distance"]
    xs = range(2)
    dvals = [summary[m]["dijkstra_nodes"] for m in ("time", "distance")]
    avals = [summary[m]["astar_nodes"] for m in ("time", "distance")]
    ax.bar([x - 0.2 for x in xs], dvals, 0.4, color=COL_D, label="Dijkstra")
    ax.bar([x + 0.2 for x in xs], avals, 0.4, color=COL_A, label="A*")
    for x, dv, av in zip(xs, dvals, avals):
        ax.text(x + 0.2, av, f"-{100 * (1 - av / dv):.0f}%", ha="center", va="bottom", fontsize=10)
    ax.set_xticks(list(xs), labels)
    _style(ax, "Average nodes explored per query", "", "nodes explored")
    fig.tight_layout()
    fig.savefig(out_dir / "speedup_by_metric.png", dpi=150)
    plt.close(fig)

    for metric, s in summary.items():
        log(f"  [{metric:8}] {s['queries']} queries | nodes  Dijkstra {s['dijkstra_nodes']:8.0f}  "
            f"A* {s['astar_nodes']:8.0f}  ({100 * (1 - s['astar_nodes'] / s['dijkstra_nodes']):.0f}% fewer) | "
            f"ms  Dijkstra {s['dijkstra_ms']:.2f}  A* {s['astar_ms']:.2f}")
    return summary


def run_scaling(out_dir: Path, sizes=(10, 32, 100, 200, 316), queries=30, seed=3, log=print):
    """Synthetic grid cities of ~100 to ~100,000 intersections."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from .graph import RoadGraph

    rows = []
    for side in sizes:
        g = RoadGraph.grid_city(side, side, seed=seed)
        rng = random.Random(seed)
        pairs = _pairs(g, queries, rng, min_m=0.0)
        res = compare(g, pairs, "time")
        row = {
            "nodes": g.n, "edges": len(g.edges),
            "dijkstra_nodes": statistics.mean(r["dijkstra_nodes"] for r in res),
            "astar_nodes": statistics.mean(r["astar_nodes"] for r in res),
            "dijkstra_ms": statistics.mean(r["dijkstra_ms"] for r in res),
            "astar_ms": statistics.mean(r["astar_ms"] for r in res),
        }
        row["vlogv_scaled"] = g.n * math.log2(max(2, g.n))
        rows.append(row)
        log(f"  grid {side}x{side}: V={g.n:6d} E={len(g.edges):6d} | nodes D {row['dijkstra_nodes']:8.0f} "
            f"A* {row['astar_nodes']:8.0f} | ms D {row['dijkstra_ms']:8.2f} A* {row['astar_ms']:8.2f}")
    _save_csv(rows, out_dir / "scaling.csv")

    for key, ylabel, fname in (("nodes", "avg nodes explored", "scaling_nodes.png"),
                               ("ms", "avg runtime (ms)", "scaling_runtime.png")):
        fig, ax = plt.subplots(figsize=(7, 4.3))
        V = [r["nodes"] for r in rows]
        ax.plot(V, [r[f"dijkstra_{key}"] for r in rows], "o-", color=COL_D, label="Dijkstra")
        ax.plot(V, [r[f"astar_{key}"] for r in rows], "o-", color=COL_A, label="A*")
        ax.set_xscale("log")
        ax.set_yscale("log")
        _style(ax, f"{ylabel.capitalize()} vs graph size (synthetic cities)", "graph size V (nodes, log scale)",
               ylabel + " (log scale)")
        fig.tight_layout()
        fig.savefig(out_dir / fname, dpi=150)
        plt.close(fig)
    return rows
