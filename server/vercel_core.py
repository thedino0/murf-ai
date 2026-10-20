"""Vercel file-function adapter for the shared Murf web application.

Vercel serves api/*.py as separate functions and public/ as static files.
Each function subclasses ApiHandler, which normalizes the function path back
to the routes understood by app.Handler.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as webapp  # noqa: E402

# Vercel caps responses at 4.5 MB. Use 2,500 characters unless the project
# explicitly sets WEB_MAX_TEXT. The local server keeps its 5,000 default.
_configured_max_text = os.environ.get("WEB_MAX_TEXT", "").strip()
application = webapp.Application(max_text=None if _configured_max_text else 2500)


class ApiHandler(webapp.Handler):
    """Map a Vercel function URL onto the application's /api routes."""

    @property
    def app(self) -> webapp.Application:
        return application

    def _normalized_path(self) -> str:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            path = "/api" + path if path.startswith("/") else "/api/" + path
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        return path + query

    def do_GET(self) -> None:
        original = self.path
        self.path = self._normalized_path()
        try:
            super().do_GET()
        finally:
            self.path = original

    def do_POST(self) -> None:
        original = self.path
        self.path = self._normalized_path()
        try:
            super().do_POST()
        finally:
            self.path = original
