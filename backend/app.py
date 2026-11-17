"""Entry point for Vercel: it looks for a Flask instance called `app` in app.py.

(Locally, use  python run.py  instead.)
"""

from aro.app import create_app
from aro.config import Config

app = create_app(Config)
