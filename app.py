"""Ambulance Route Optimizer - entry point.

Vercel:  finds the Flask instance `app` in this file automatically.
Locally: python app.py   ->  http://127.0.0.1:5000
"""

import os

from aro.config import Config
from aro.server import create_app

app = create_app(Config)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"\n  Ambulance Route Optimizer:  http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
