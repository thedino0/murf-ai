"""End-to-end checks for the local web app.

Run from the repository root:

    py -3 tests/test_web.py

The test starts a mock Murf endpoint and a real app server, then talks HTTP to
both. It does not call Murf and does not need an API key.
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import murf  # noqa: E402
import server.vercel_core as vercel_core  # noqa: E402
from tests.test_free_client import AUDIO, Handler  # noqa: E402


def call(base, path, payload=None):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def decode(body):
    return json.loads(body.decode("utf-8"))


def main():
    mock = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    client = murf.MurfClient(
        base_url=f"http://127.0.0.1:{mock.server_port}",
        retry=murf.RetryPolicy(attempts=2, timeout=5, backoff=0.01),
    )
    application = app.Application(
        client=client,
        max_text=5000,
        rate_units=0,
        max_concurrent=1,
        chunk_delay=0,
    )
    web = app.make_server("127.0.0.1", 0, application)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{web.server_port}"

    try:
        status, headers, body = call(base, "/")
        assert status == 200
        assert "text/html" in headers["Content-Type"]
        assert b'id="generate"' in body
        assert "default-src 'self'" in headers["Content-Security-Policy"]

        status, headers, body = call(base, "/static/app.js")
        assert status == 200
        assert "javascript" in headers["Content-Type"]
        assert b"murf-web-settings-v1" in body

        status, headers, body = call(base, "/healthz")
        assert status == 200
        assert decode(body)["ok"] is True

        head = urllib.request.Request(base + "/healthz", method="HEAD")
        with urllib.request.urlopen(head, timeout=15) as response:
            assert response.status == 200
            assert response.read() == b""

        status, headers, body = call(base, "/api/config")
        config = decode(body)
        assert status == 200
        assert config["maxChars"] == 5000
        assert config["maxChunk"] == 500

        status, headers, body = call(base, "/api/voices")
        catalog = decode(body)
        assert status == 200
        assert catalog["count"] == 3
        assert sum(voice["tier"] == "PREMIUM" for voice in catalog["voices"]) == 1

        status, headers, body = call(
            base,
            "/api/synthesize",
            {
                "text": "hello from the web",
                "voice": "en-US-test",
                "rate": 5,
                "pitch": -5,
                "locale": "fr_FR",
            },
        )
        assert status == 200, body
        assert body == AUDIO
        assert headers["Content-Type"] == "audio/mpeg"
        assert headers["X-Murf-Chunks"] == "1"
        assert headers["X-Murf-Bytes"] == str(len(AUDIO))
        assert headers["Content-Disposition"].endswith('.mp3"')

        from tests.test_free_client import STATE

        assert STATE["last_query"]["rate"] == "5"
        assert STATE["last_query"]["pitch"] == "-5"
        assert STATE["last_query"]["multiNativeLocale"] == "fr-FR"

        status, headers, body = call(
            base,
            "/api/synthesize",
            {"text": "limit this", "voice": "en-US-premium"},
        )
        limit = decode(body)
        assert status == 429
        assert limit["error"] == "limit"
        assert headers["Retry-After"] == "60"

        status, headers, body = call(
            base,
            "/api/synthesize",
            {"text": "bad rate", "voice": "en-US-test", "rate": 51},
        )
        assert status == 400
        assert "rate" in decode(body)["message"]

        status, headers, body = call(
            base,
            "/api/synthesize",
            {"text": "x" * 5001, "voice": "en-US-test"},
        )
        assert status == 400
        assert "allows 5000" in decode(body)["message"]

        status, headers, body = call(
            base,
            "/api/synthesize",
            {"text": "hello", "voice": "does-not-exist"},
        )
        assert status == 400
        assert decode(body)["error"] == "voice"

        limiter = app.RateLimiter(1, 60)
        assert limiter.reserve("visitor", 1) is None
        assert limiter.reserve("visitor", 1) >= 1
        assert limiter.reserve("other", 1) is None

        # Exercise the Vercel adapter with the same mock client. Vercel
        # normally calls /api/*, but the adapter also accepts /healthz.
        vercel_core.application = app.Application(
            client=client,
            max_text=5000,
            rate_units=0,
            max_concurrent=1,
            chunk_delay=0,
        )
        route = object.__new__(vercel_core.ApiHandler)
        route.path = "/voices?limit=1"
        assert route._normalized_path() == "/api/voices?limit=1"
        route.path = "/api/healthz/"
        assert route._normalized_path() == "/api/healthz"

        vercel_web = ThreadingHTTPServer(("127.0.0.1", 0), vercel_core.ApiHandler)
        threading.Thread(target=vercel_web.serve_forever, daemon=True).start()
        vbase = f"http://127.0.0.1:{vercel_web.server_port}"
        try:
            status, headers, body = call(vbase, "/api/config")
            assert status == 200
            assert decode(body)["maxChars"] == 5000

            status, headers, body = call(vbase, "/healthz")
            assert status == 200
            assert decode(body)["ok"] is True

            status, headers, body = call(vbase, "/api/voices")
            assert status == 200
            assert decode(body)["count"] == 3

            status, headers, body = call(
                vbase,
                "/api/synthesize",
                {"text": "hello from vercel", "voice": "en-US-test"},
            )
            assert status == 200, body
            assert body == AUDIO
            assert headers["Content-Type"] == "audio/mpeg"
        finally:
            vercel_web.shutdown()
            vercel_web.server_close()

        print("all web checks passed")
        return 0
    finally:
        web.shutdown()
        web.server_close()
        mock.shutdown()
        mock.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
