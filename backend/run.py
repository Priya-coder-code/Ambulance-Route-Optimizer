#!/usr/bin/env python3
"""Start the backend:   python run.py

First start: creates the PostgreSQL tables, downloads the road network of the
service area with OSMnx (cached afterwards), fetches real hospitals from
OpenStreetMap and creates the simulated ambulance fleet.
"""

import argparse

from aro.app import create_app
from aro.config import Config


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    args = p.parse_args()
    try:
        app = create_app(Config)
    except Exception as e:  # most often: PostgreSQL is not running
        print(f"\nCould not start: {e.__class__.__name__}: {str(e).splitlines()[0]}")
        print(f"Is PostgreSQL running and reachable at {Config.DATABASE_URL.split('@')[-1]}?")
        print("Start it with:  docker compose up -d   (or set DATABASE_URL in backend/.env)\n")
        raise SystemExit(1)
    print(f"\n  Ambulance Route Optimizer API on http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
