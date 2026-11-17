#!/usr/bin/env python3
"""Database / data management.

  python manage.py init-db       create the tables + emergency types
  python manage.py download      download & cache the road network now
  python manage.py seed          create hospitals (from OpenStreetMap) and ambulances
  python manage.py reset         DELETE all data and seed again
  python manage.py stats         row counts
  python manage.py export-graph  write backend/deploy/roadnet.pkl.gz for a Vercel deployment

To fill the Neon database used on Vercel, run "seed" on your computer with
DATABASE_URL set to the Neon connection string (in backend/.env).
"""

import argparse

from sqlalchemy import func, select

from aro.config import Config
from aro.db import drop_all, init_engine, session_scope
from aro.models import Ambulance, Dispatch, Emergency, Hospital, RoadCondition
from aro.road_network import RoadNetwork
from aro.seed import seed_ambulances, seed_emergency_types, seed_hospitals


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["init-db", "download", "seed", "reset", "stats", "export-graph"])
    cmd = p.parse_args().command
    init_engine(Config.DATABASE_URL)

    if cmd == "reset":
        if input("This deletes ALL hospitals, ambulances and dispatch history. Type 'yes': ") != "yes":
            return
        drop_all()
    if cmd in ("init-db", "reset", "seed"):
        with session_scope() as s:
            seed_emergency_types(s)
        print("Tables ready.")
    if cmd in ("download", "seed", "reset"):
        net = RoadNetwork.load(Config)
        print(f"Road network: {net.n:,} intersections, {net.m:,} edges")
        if cmd != "download":
            with session_scope() as s:
                seed_hospitals(s, Config)
            with session_scope() as s:
                seed_ambulances(s, net, Config)
    if cmd == "export-graph":
        import time
        net = RoadNetwork.load(Config)
        size = net.export(Config.ROAD_NETWORK_FILE)
        t0 = time.time()
        RoadNetwork.load_file(Config.ROAD_NETWORK_FILE)
        print(f"Wrote {Config.ROAD_NETWORK_FILE} : {size / 1e6:.1f} MB "
              f"({net.n:,} intersections, {net.m:,} edges; loads in {time.time() - t0:.1f}s)")
        if size > 95e6:
            print("WARNING: over GitHub's 100 MB file limit - use a smaller AREA_SIZE_KM for the deployment.")
    if cmd == "stats":
        with session_scope() as s:
            for model in (Hospital, Ambulance, Emergency, Dispatch, RoadCondition):
                print(f"{model.__tablename__:16} {s.scalar(select(func.count()).select_from(model))}")


if __name__ == "__main__":
    main()
