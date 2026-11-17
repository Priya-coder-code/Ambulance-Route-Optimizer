#!/usr/bin/env python3
"""Small maintenance commands (use the same DATABASE_URL as the app).

  python manage.py stats        row counts
  python manage.py refresh-map  forget the stored road map; it is downloaded again on the next start
  python manage.py reset        DELETE all data; hospitals, ambulances and map are re-created on the next start
"""

import argparse

from sqlalchemy import delete, func, select

from aro.config import Config
from aro.db import drop_all, init_engine, session_scope
from aro.models import Ambulance, Dispatch, Emergency, Hospital, OsmCache, RoadCondition


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["stats", "refresh-map", "reset"])
    cmd = p.parse_args().command
    init_engine(Config.DATABASE_URL)
    print(f"Database: {Config.DB_MODE}")
    if cmd == "reset":
        if input("Delete ALL data (hospitals, ambulances, cases)? Type 'yes': ") != "yes":
            return
        drop_all()
        print("Done. Everything is re-created on the next start of the app.")
    elif cmd == "refresh-map":
        with session_scope() as s:
            s.execute(delete(OsmCache))
        print("Stored road map removed. Restart / redeploy the app to download it again.")
    else:
        with session_scope() as s:
            for model in (Hospital, Ambulance, Emergency, Dispatch, RoadCondition, OsmCache):
                print(f"{model.__tablename__:16} {s.scalar(select(func.count()).select_from(model))}")


if __name__ == "__main__":
    main()
