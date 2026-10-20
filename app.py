#!/usr/bin/env python3
"""Render-ready web frontend for the keyless Murf client.

The browser UI posts a small JSON document to /api/synthesize. The server
resolves the voice, chunks long text with murf.split_text, writes the joined
MP3 to a temporary file, and returns the bytes. No user text is logged or
stored after the response.

Run locally:
    python app.py
    # http://127.0.0.1:10000

Render sets PORT and expects the process to bind 0.0.0.0.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import murf

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
APP_NAME = "murf-web"
VERSION = "1.0.0"
DEFAULT_PORT = 10000
MAX_BODY_BYTES = 64 * 1024
LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data:; "
    "media-src 'self' blob:; "
    "script-src 'self'; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "worker-src 'none'"
)

logger = logging.getLogger(APP_NAME)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("ignoring non-integer %s=%r", name, raw)
        return default
    return min(maximum, max(minimum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ignoring non-number %s=%r", name, raw)
        return default
    return min(maximum, max(minimum, value))


def _utf16_units(text: str) -> int:
    """Match the browser's string length and Murf's effective limit."""
    return len(text.encode("utf-16-le")) // 2


def _safe_header(value: str, fallback: str = "speech") -> str:
    cleaned = SAFE_NAME_RE.sub("-", value).strip("-_")
    return cleaned[:60] or fallback


def _murf_message(exc: murf.MurfError) -> str:
    if exc.detail:
        return f"{exc} ({exc.detail})"
    return str(exc)


class WebError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class SynthesisRequest:
    text: str
    voice_alias: str
    style: str | None
    rate: int
    pitch: int
    locale: str | None
    chunks: int


class RateLimiter:
    """Small in-process limiter measured in audio chunks rather than jobs."""

    def __init__(self, max_units: int, window_seconds: float):
        self.max_units = int(max_units)
        self.window = float(window_seconds)
        self._hits: dict[str, deque[tuple[float, int]]] = defaultdict(deque)
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.max_units > 0 and self.window > 0

    def reserve(self, key: str, units: int) -> int | None:
        """Reserve units. Return retry-after seconds when rejected."""
        if not self.enabled:
            return None

        units = max(1, int(units))
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0][0] < cutoff:
                bucket.popleft()

            used = sum(cost for _, cost in bucket)
            if used + units > self.max_units:
                if not bucket:
                    return max(1, math.ceil(self.window))
                return max(1, math.ceil(bucket[0][0] + self.window - now))

            bucket.append((now, units))
            if len(self._hits) > 2048:
                for stale_key in list(self._hits):
                    stale = self._hits[stale_key]
                    while stale and stale[0][0] < cutoff:
                        stale.popleft()
                    if not stale:
                        self._hits.pop(stale_key, None)
            return None


class Application:
    def __init__(
        self,
        *,
        client: murf.MurfClient | None = None,
        max_text: int | None = None,
        rate_units: int | None = None,
        rate_window: float | None = None,
        max_concurrent: int | None = None,
        chunk_delay: float | None = None,
    ):
        retry = murf.RetryPolicy(
            attempts=3,
            timeout=45.0,
            backoff=0.6,
            max_backoff=8.0,
        )
        base_url = os.environ.get("MURF_BASE_URL", murf.DEFAULT_BASE_URL)
        self.client = client or murf.MurfClient(base_url=base_url, retry=retry)
        self.max_text = int(
            max_text
            if max_text is not None
            else _env_int("WEB_MAX_TEXT", 5000, murf.MAX_CHUNK_CHARS, 50000)
        )
        self.chunk_delay = float(
            chunk_delay
            if chunk_delay is not None
            else _env_float("WEB_CHUNK_DELAY", 0.15, 0.0, 5.0)
        )
        self.limiter = RateLimiter(
            rate_units
            if rate_units is not None
            else _env_int("WEB_RATE_CHUNKS", 40, 0, 100000),
            rate_window
            if rate_window is not None
            else _env_float("WEB_RATE_WINDOW", 900.0, 1.0, 86400.0),
        )
        self._catalog_lock = threading.Lock()
        self._catalog: list[murf.Voice] | None = None
        concurrency = int(
            max_concurrent
            if max_concurrent is not None
            else _env_int("WEB_MAX_CONCURRENT", 2, 1, 8)
        )
        self._semaphore = threading.BoundedSemaphore(max(1, concurrency))

    def voices(self) -> list[murf.Voice]:
        with self._catalog_lock:
            if self._catalog is None:
                self._catalog = list(self.client.voices())
            return self._catalog

    def resolve_voice(self, alias: str) -> murf.Voice:
        self.voices()
        return self.client.resolve_voice(alias)

    def parse_request(self, payload: Mapping[str, Any]) -> SynthesisRequest:
        if not isinstance(payload, Mapping):
            raise WebError(HTTPStatus.BAD_REQUEST, "json", "Send a JSON object.")

        raw_text = payload.get("text")
        if not isinstance(raw_text, str):
            raise WebError(HTTPStatus.BAD_REQUEST, "text", "Text must be a string.")
        text = raw_text.strip()
        if not text:
            raise WebError(HTTPStatus.BAD_REQUEST, "text", "Add some text first.")

        units = _utf16_units(text)
        if units > self.max_text:
            raise WebError(
                HTTPStatus.BAD_REQUEST,
                "text_too_long",
                f"Text is {units} characters; this site allows {self.max_text}.",
            )

        raw_voice = payload.get("voice") or murf.DEFAULT_VOICE
        if not isinstance(raw_voice, str) or len(raw_voice.strip()) > 80:
            raise WebError(HTTPStatus.BAD_REQUEST, "voice", "Choose a valid voice.")
        voice_alias = raw_voice.strip() or murf.DEFAULT_VOICE

        raw_style = payload.get("style")
        if raw_style is None:
            style = None
        elif not isinstance(raw_style, str):
            raise WebError(HTTPStatus.BAD_REQUEST, "style", "Style must be a string.")
        else:
            style = raw_style.strip() or None
            if style and len(style) > 80:
                raise WebError(HTTPStatus.BAD_REQUEST, "style", "Style is too long.")

        rate = _adjustment(payload.get("rate", 0), "rate")
        pitch = _adjustment(payload.get("pitch", 0), "pitch")

        raw_locale = payload.get("locale")
        if raw_locale is None:
            locale = None
        elif not isinstance(raw_locale, str):
            raise WebError(HTTPStatus.BAD_REQUEST, "locale", "Locale must be a string.")
        else:
            locale = raw_locale.strip() or None
            if locale and (len(locale) > 24 or not LOCALE_RE.fullmatch(locale)):
                raise WebError(
                    HTTPStatus.BAD_REQUEST,
                    "locale",
                    "Use a locale such as en-US or fr-FR.",
                )

        return SynthesisRequest(
            text=text,
            voice_alias=voice_alias,
            style=style,
            rate=rate,
            pitch=pitch,
            locale=locale,
            chunks=len(murf.split_text(text)),
        )

    def synthesize(
        self,
        request: SynthesisRequest,
        voice: murf.Voice,
    ) -> tuple[bytes, int, float]:
        if not self._semaphore.acquire(timeout=30):
            raise WebError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "busy",
                "The voice queue is full. Try again in a moment.",
            )
        try:
            started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="murf-web-") as temp:
                destination = Path(temp) / "speech.mp3"
                written, chunks = murf._atomic_generate(
                    self.client,
                    request.text,
                    voice,
                    request.style,
                    destination,
                    delay=self.chunk_delay,
                    quiet=True,
                    label="",
                    rate=request.rate,
                    pitch=request.pitch,
                    locale=request.locale,
                )
                audio = destination.read_bytes()
            if written != len(audio):
                raise WebError(
                    HTTPStatus.BAD_GATEWAY,
                    "audio",
                    "The generated audio was incomplete.",
                )
            return audio, chunks, time.monotonic() - started
        finally:
            self._semaphore.release()


def _adjustment(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WebError(
            HTTPStatus.BAD_REQUEST,
            name,
            f"{name} must be an integer between "
            f"{murf.ADJUSTMENT_MIN} and {murf.ADJUSTMENT_MAX}.",
        )
    if not murf.ADJUSTMENT_MIN <= value <= murf.ADJUSTMENT_MAX:
        raise WebError(
            HTTPStatus.BAD_REQUEST,
            name,
            f"{name} must be between {murf.ADJUSTMENT_MIN} and {murf.ADJUSTMENT_MAX}.",
        )
    return value


class MurfWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        application: Application,
    ):
        self.application = application
        super().__init__(address, handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = APP_NAME

    @property
    def app(self) -> Application:
        return self.server.application  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s %s", self.address_string(), fmt % args)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        super().end_headers()

    def _write(self, body: bytes) -> None:
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cache: str = "no-store",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self._write(body)

    def _send_json(
        self,
        status: int,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            headers=headers,
        )

    def _send_web_error(self, exc: WebError) -> None:
        payload: dict[str, Any] = {"error": exc.code, "message": exc.message}
        headers: dict[str, str] = {}
        if exc.retry_after is not None:
            payload["retry_after"] = exc.retry_after
            headers["Retry-After"] = str(exc.retry_after)
        self._send_json(exc.status, payload, headers=headers)

    def _file(self, path: Path, *, content_type: str | None = None, cache: str = "no-cache") -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "not_found", "message": "File not found."},
            )
            return
        ctype = content_type or STATIC_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self._send_bytes(HTTPStatus.OK, body, ctype, cache=cache)

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            candidate = forwarded.split(",", 1)[0].strip()
            if candidate:
                return candidate[:64]
        return (self.client_address[0] if self.client_address else "unknown")[:64]

    def _static_path(self, request_path: str) -> Path | None:
        relative = urllib.parse.unquote(request_path[len("/static/") :])
        if not relative or ".." in Path(relative).parts:
            return None
        candidate = (WEB_ROOT / relative).resolve()
        try:
            candidate.relative_to(WEB_ROOT.resolve())
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        try:
            if path == "/healthz":
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "service": APP_NAME, "version": VERSION},
                )
                return

            if path in ("/", "/index.html"):
                self._file(WEB_ROOT / "index.html", content_type="text/html; charset=utf-8")
                return

            if path.startswith("/static/"):
                static = self._static_path(path)
                if static is None:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "not_found", "message": "File not found."},
                    )
                    return
                self._file(static)
                return

            if path == "/api/voices":
                voices = self.app.voices()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "voices": [_voice_dict(voice) for voice in voices],
                        "count": len(voices),
                    },
                )
                return

            if path == "/api/config":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "version": VERSION,
                        "clientVersion": murf.VERSION,
                        "maxChars": self.app.max_text,
                        "maxChunk": murf.MAX_CHUNK_CHARS,
                        "adjustmentMin": murf.ADJUSTMENT_MIN,
                        "adjustmentMax": murf.ADJUSTMENT_MAX,
                        "rateChunks": self.app.limiter.max_units,
                        "rateWindow": self.app.limiter.window,
                    },
                )
                return

            if path == "/favicon.ico":
                self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
                return

            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "not_found", "message": "Path not found."},
            )
        except murf.MurfError as exc:
            logger.warning("catalog request failed: %s", exc)
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": "upstream", "message": _murf_message(exc)},
            )
        except Exception:
            logger.exception("GET %s failed", path)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "server", "message": "Unexpected server error."},
            )

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/healthz":
            body = json.dumps(
                {"ok": True, "service": APP_NAME, "version": VERSION},
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/api/synthesize":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "not_found", "message": "Path not found."},
            )
            return

        try:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type and content_type != "application/json":
                raise WebError(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "content_type",
                    "Use Content-Type: application/json.",
                )

            raw_length = self.headers.get("Content-Length", "")
            try:
                length = int(raw_length)
            except ValueError:
                raise WebError(HTTPStatus.LENGTH_REQUIRED, "length", "Content-Length is required.")
            if length <= 0:
                raise WebError(HTTPStatus.BAD_REQUEST, "empty", "Request body is empty.")
            if length > MAX_BODY_BYTES:
                raise WebError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "large",
                    "Request body is too large.",
                )

            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise WebError(HTTPStatus.BAD_REQUEST, "json", "Request body is not valid JSON.")

            request = self.app.parse_request(payload)
            voice = self.app.resolve_voice(request.voice_alias)

            retry_after = self.app.limiter.reserve(self._client_ip(), request.chunks)
            if retry_after is not None:
                raise WebError(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "rate_limited",
                    "This browser has used its share of the public voice budget.",
                    retry_after=retry_after,
                )

            try:
                audio, chunks, elapsed = self.app.synthesize(request, voice)
            except murf.MurfLimitError as exc:
                logger.warning("upstream call budget exhausted: %s", exc)
                raise WebError(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "limit",
                    "Murf's public endpoint hit its call budget. Wait a few minutes "
                    "or use the official API client.",
                    retry_after=60,
                ) from exc
            except murf.MurfError as exc:
                logger.warning("synthesis failed: %s", exc)
                raise WebError(
                    HTTPStatus.BAD_GATEWAY,
                    "upstream",
                    _murf_message(exc),
                ) from exc

            filename = f"murf-{_safe_header(voice.api_name or voice.voice_id)}.mp3"
            self._send_bytes(
                HTTPStatus.OK,
                audio,
                "audio/mpeg",
                headers={
                    "Content-Disposition": f'inline; filename="{filename}"',
                    "X-Murf-Chunks": str(chunks),
                    "X-Murf-Bytes": str(len(audio)),
                    "X-Murf-Elapsed": f"{elapsed:.2f}",
                    "X-Murf-Voice": _safe_header(voice.api_name or voice.voice_id),
                },
            )
            return

        except WebError as exc:
            self._send_web_error(exc)
        except murf.MurfError as exc:
            logger.warning("voice lookup failed: %s", exc)
            self._send_web_error(
                WebError(
                    HTTPStatus.BAD_REQUEST,
                    "voice",
                    _murf_message(exc),
                )
            )
        except Exception:
            logger.exception("POST /api/synthesize failed")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "server", "message": "Unexpected server error."},
            )


def _voice_dict(voice: murf.Voice) -> dict[str, Any]:
    return {
        "voiceId": voice.voice_id,
        "apiName": voice.api_name,
        "name": voice.name,
        "gender": voice.gender,
        "locale": voice.locale,
        "language": voice.language,
        "styles": list(voice.styles),
        "tier": voice.tier,
    }


def make_server(
    host: str,
    port: int,
    application: Application | None = None,
) -> MurfWebServer:
    return MurfWebServer((host, port), Handler, application or Application())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render-ready keyless Murf web app")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="bind address (default: $HOST or 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_int("PORT", DEFAULT_PORT, 1, 65535),
        help="port (default: $PORT or 10000)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    application = Application()
    server = make_server(args.host, args.port, application)
    logger.info("listening on http://%s:%s", args.host, server.server_port)
    logger.info(
        "max text=%s chunks, per-IP=%s chunks/%ss",
        application.max_text,
        application.limiter.max_units,
        int(application.limiter.window),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
