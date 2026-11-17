#!/usr/bin/env python3
"""Ambulance Route Optimizer - Dijkstra vs A* on real OpenStreetMap data.

Default map: Central Delhi, a 50 km x 50 km square centred on Connaught Place,
downloaded from the OpenStreetMap Overpass API (first run only; then cached).

Examples
  python main.py web                                    # Delhi 50x50 km, opens the map app
  python main.py web --size 10                          # smaller 10x10 km Delhi (faster)
  python main.py web --place "Koramangala, Bengaluru" --size 8
  python main.py web --center 28.5672,77.2100 --size 20
  python main.py web --osm-file map.osm                 # offline, file exported from openstreetmap.org
  python main.py web --sample                           # bundled offline sample (no internet)
  python main.py dispatch --incident 28.6315,77.2167    # text-mode emergency dispatch
  python main.py route --from 28.6315,77.2167 --to 28.5672,77.2100
  python main.py benchmark                              # experiments + charts for the report
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from aro.area import DELHI_CENTER, DELHI_NAME, DELHI_SIZE_KM, AreaSpec, load_area, make_dispatcher  # noqa: E402


def latlon(text: str):
    a, b = text.split(",")
    return float(a), float(b)


def spec_from_args(args) -> AreaSpec:
    if args.place:
        center, name = None, None
    elif args.center:
        center = latlon(args.center)
        name = f"{args.size:g} x {args.size:g} km around {center[0]:.4f}, {center[1]:.4f}"
    else:
        center = DELHI_CENTER
        name = DELHI_NAME if args.size == DELHI_SIZE_KM else \
            f"Central Delhi ({args.size:g} x {args.size:g} km around Connaught Place)"
    return AreaSpec(center=center, size_km=args.size, place=args.place, name=name,
                    osm_file=args.osm_file, sample=args.sample, pois_file=args.pois,
                    include_service=args.include_service, ignore_oneway=args.ignore_oneway,
                    refresh=args.refresh, tile_km=args.tile_km)


def build(args):
    spec = spec_from_args(args)
    try:
        g, pois = load_area(spec)
    except Exception as e:
        if spec.osm_file or spec.sample:
            raise
        print(f"\n!! Could not download OpenStreetMap data: {e}")
        print("!! Check your internet connection and run again (finished tiles are kept).")
        print("!! Using the bundled offline sample map for now.\n")
        spec.sample = True
        g, pois = load_area(spec)
    d = make_dispatcher(g, pois)
    s = g.summary()
    print(f"Graph ready: {s['nodes']:,} intersections, {s['edges']:,} directed edges, "
          f"{s['road_km']:,} km of road, {len(d.hospitals)} hospitals, {len(d.ambulances)} ambulances")
    return g, d, pois


def fmt_time(s):
    return f"{int(s // 60)} min {round(s % 60)} s"


def print_compare(rd, ra):
    print(f"    {'':18}{'Dijkstra':>12}{'A*':>12}")
    for label, k in (("nodes explored", "nodes_explored"), ("edges relaxed", "edges_relaxed"),
                     ("heap pushes", "heap_pushes"), ("max frontier", "max_frontier")):
        print(f"    {label:18}{getattr(rd, k):>12,}{getattr(ra, k):>12,}")
    print(f"    {'runtime (ms)':18}{rd.runtime_ms:>12.2f}{ra.runtime_ms:>12.2f}")
    same = abs(rd.cost - ra.cost) < 1e-6 * max(1, rd.cost)
    print(f"    same optimal cost: {'YES' if same else 'NO'}   "
          f"A* explored {100 * (1 - ra.nodes_explored / max(1, rd.nodes_explored)):.0f}% fewer nodes")


def cmd_web(args):
    from aro.web.app import serve

    g, _, pois = build(args)
    if not args.no_browser:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    serve(g, pois, port=args.port)


def cmd_route(args):
    from aro.algorithms import astar, dijkstra

    g, _, _ = build(args)
    s, _ = g.nearest_node(*latlon(args.src))
    t, _ = g.nearest_node(*latlon(args.dst))
    rd = dijkstra(g, s, t, args.metric, record=False)
    ra = astar(g, s, t, args.metric, record=False)
    if not ra.found:
        print("No route found.")
        return
    st = g.path_stats(ra.path_edges)
    print(f"\nRoute: {st['distance_m'] / 1000:.2f} km, {fmt_time(st['time_s'])}")
    print("  via " + " -> ".join(st["roads"][:12]))
    print_compare(rd, ra)


def cmd_dispatch(args):
    g, d, _ = build(args)
    lat, lon = latlon(args.incident)
    out = d.dispatch(lat, lon, metric=args.metric, record=False)
    (r1d, _), (r2d, _) = out["legs"]["dijkstra"]["to_patient"], out["legs"]["dijkstra"]["to_hospital"]
    (r1a, amb), (r2a, hosp) = out["legs"]["astar"]["to_patient"], out["legs"]["astar"]["to_hospital"]
    s1, s2 = g.path_stats(r1a.path_edges), g.path_stats(r2a.path_edges)
    print(f"\nEMERGENCY at {lat:.5f}, {lon:.5f}")
    print(f"  1. {amb.name} -> patient: {fmt_time(s1['time_s'])}, {s1['distance_m'] / 1000:.2f} km")
    print_compare(r1d, r1a)
    print(f"  2. patient -> {hosp.name}: {fmt_time(s2['time_s'])}, {s2['distance_m'] / 1000:.2f} km")
    print("     via " + " -> ".join(s2["roads"][:10]))
    print_compare(r2d, r2a)
    print(f"  TOTAL response time: {fmt_time(s1['time_s'] + s2['time_s'])}")


def cmd_benchmark(args):
    from aro.benchmark import run_real, run_scaling

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    g, _, _ = build(args)
    print(f"\nReal map: {args.queries} random queries x 2 metrics")
    run_real(g, out, queries=args.queries)
    if not args.skip_scaling:
        print("\nSynthetic cities (scaling experiment)")
        run_scaling(out)
    print(f"\nCharts and CSVs written to {out.resolve()}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def area_args(sp):
        sp.add_argument("--size", type=float, default=DELHI_SIZE_KM,
                        help=f"side of the square area in km (default {DELHI_SIZE_KM})")
        sp.add_argument("--center", help="lat,lon of the area centre (default: Connaught Place, Delhi)")
        sp.add_argument("--place", help="place name to centre on instead (looked up on OpenStreetMap)")
        sp.add_argument("--osm-file", help="use a local .osm file instead of downloading")
        sp.add_argument("--sample", action="store_true", help="use the bundled offline sample map")
        sp.add_argument("--pois", help="JSON file with extra hospitals / ambulance bases")
        sp.add_argument("--include-service", action="store_true",
                        help="also load service roads (lanes, campus roads); bigger graph")
        sp.add_argument("--ignore-oneway", action="store_true", help="let ambulances drive against one-way streets")
        sp.add_argument("--refresh", action="store_true", help="re-download even if cached")
        sp.add_argument("--tile-km", type=float, default=10, help="download tile size in km (default 10)")
        sp.add_argument("--metric", choices=["time", "distance"], default="time")

    sp = sub.add_parser("web", help="interactive map app (Dijkstra vs A* side by side)")
    area_args(sp)
    sp.add_argument("--port", type=int, default=5000)
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(fn=cmd_web)

    sp = sub.add_parser("route", help="shortest route between two lat,lon points")
    area_args(sp)
    sp.add_argument("--from", dest="src", required=True, help="lat,lon")
    sp.add_argument("--to", dest="dst", required=True, help="lat,lon")
    sp.set_defaults(fn=cmd_route)

    sp = sub.add_parser("dispatch", help="emergency at lat,lon: nearest ambulance + nearest hospital")
    area_args(sp)
    sp.add_argument("--incident", required=True, help="lat,lon")
    sp.set_defaults(fn=cmd_dispatch)

    sp = sub.add_parser("benchmark", help="experiments + charts for the report")
    area_args(sp)
    sp.add_argument("--queries", type=int, default=100)
    sp.add_argument("--out", default=str(ROOT / "results"))
    sp.add_argument("--skip-scaling", action="store_true")
    sp.set_defaults(fn=cmd_benchmark)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
