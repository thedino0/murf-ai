"""GET /api/healthz on Vercel, also served at /healthz by vercel.json."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.vercel_core import ApiHandler  # noqa: E402


class handler(ApiHandler):
    pass
