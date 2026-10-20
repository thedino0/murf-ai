#!/usr/bin/env python3
"""Murf.ai text-to-speech client for the official REST API.

Requires a Murf API key. For the keyless client that uses Murf's public
widget endpoint, run murf.py instead.

Talks to the Murf REST API directly: voices, single generations, streaming,
and batch jobs from a text file. Standard library only.

Auth:
    export MURF_API_KEY=...          # required
    export MURF_VOICE_ID=...         # optional default voice

Examples:
    python murf_api.py voices --locale en-US --gender female
    python murf_api.py speak "Hello from the couch." --voice en-US-natalie -o hello.mp3
    python murf_api.py speak --file script.txt --voice en-US-natalie --format WAV
    python murf_api.py batch lines.txt --voice en-US-natalie -o out --prefix clip

Python 3.10+.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import http.client
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urljoin

APP_NAME = "murf-cli"
VERSION = "1.0.0"
USER_AGENT = f"{APP_NAME}/{VERSION}"

ENV_API_KEY = "MURF_API_KEY"
ENV_VOICE_ID = "MURF_VOICE_ID"
ENV_BASE_URL = "MURF_BASE_URL"

DEFAULT_BASE_URL = "https://api.murf.ai"
REGION_HOSTS = {
    "us": "https://api.murf.ai",
    "eu": "https://api.eu.murf.ai",
    "in": "https://api.in.murf.ai",
    "au": "https://api.au.murf.ai",
}

API_VOICES = "/v1/speech/voices"
API_GENERATE = "/v1/speech/generate"
API_STREAM = "/v1/speech/stream"

MAX_TEXT_CHARS = 3000
AUDIO_FORMATS = ("MP3", "WAV", "FLAC", "ALAW", "ULAW")
FORMAT_EXTENSIONS = {
    "MP3": ".mp3",
    "WAV": ".wav",
    "FLAC": ".flac",
    "ALAW": ".alaw",
    "ULAW": ".ulaw",
}
SAMPLE_RATES = (8000, 16000, 24000, 44100, 48000)
CHANNEL_TYPES = ("MONO", "STEREO")
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
DOWNLOAD_CHUNK = 64 * 1024
SLUG_LIMIT = 60
READ_ERRORS = (OSError, http.client.HTTPException, ValueError)


class MurfError(RuntimeError):
    """API, network, or payload failure."""

    def __init__(self, message: str, *, status: int | None = None, detail: str | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    timeout: float = 60.0
    backoff: float = 0.8
    max_backoff: float = 15.0


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    voice_id: str
    audio_format: str = "MP3"
    sample_rate: int = 24000
    channel_type: str = "MONO"
    style: str | None = None
    rate: int | None = None
    pitch: int | None = None
    variation: int | None = None
    encode_as_base64: bool = False
    multi_native_locale: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "voiceId": self.voice_id,
            "text": self.text,
            "format": self.audio_format,
            "sampleRate": self.sample_rate,
            "channelType": self.channel_type,
            "encodeAsBase64": self.encode_as_base64,
        }
        optional = {
            "style": self.style,
            "rate": self.rate,
            "pitch": self.pitch,
            "variation": self.variation,
            "multiNativeLocale": self.multi_native_locale,
        }
        for key, value in optional.items():
            if value is not None:
                payload[key] = value
        return payload


@dataclass(frozen=True)
class SpeechResult:
    payload: Mapping[str, Any]

    @property
    def audio_seconds(self) -> float | None:
        value = self.payload.get("audioLengthInSeconds")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def remaining_characters(self) -> int | None:
        value = self.payload.get("remainingCharacterCount")
        return int(value) if isinstance(value, int) else None


class MurfClient:
    """Thin, retrying client for the Murf speech endpoints."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        retry: RetryPolicy | None = None,
    ):
        if not api_key:
            raise MurfError(f"missing API key (set {ENV_API_KEY} or pass --api-key)")
        self._base_url = base_url.rstrip("/")
        self._retry = retry or RetryPolicy()
        self._auth_headers = {
            "api-key": api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        # Signed audio URLs must never see the API key.
        self._plain_headers = {"User-Agent": USER_AGENT}

    def __enter__(self) -> "MurfClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        pass

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        authorized: bool = True,
    ) -> Any:
        target = url if "://" in url else urljoin(self._base_url + "/", url.lstrip("/"))
        if params:
            separator = "&" if "?" in target else "?"
            target = f"{target}{separator}{urllib.parse.urlencode(params)}"

        headers = dict(self._auth_headers if authorized else self._plain_headers)
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_error = "unknown error"
        for attempt in range(1, self._retry.attempts + 1):
            request = urllib.request.Request(target, data=body, method=method, headers=headers)
            try:
                return urllib.request.urlopen(request, timeout=self._retry.timeout)
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    detail = _error_detail(exc.read())
                except READ_ERRORS:
                    detail = None
                retry_after = _parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers else None
                )
                exc.close()
                if status in RETRY_STATUS and attempt < self._retry.attempts:
                    self._sleep(attempt, retry_after)
                    continue
                raise MurfError(
                    f"HTTP {status} from {target}", status=status, detail=detail
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(getattr(exc, "reason", exc))
                if attempt >= self._retry.attempts:
                    break
                self._sleep(attempt)

        raise MurfError(
            f"request failed after {self._retry.attempts} attempt(s): {last_error}"
        )

    def _sleep(self, attempt: int, retry_after: float | None = None) -> None:
        if retry_after is not None:
            delay = min(retry_after, 60.0)
        else:
            delay = min(
                self._retry.backoff * (2 ** (attempt - 1)),
                self._retry.max_backoff,
            )
        if delay > 0:
            time.sleep(delay + random.uniform(0.0, delay * 0.25))

    @staticmethod
    def _json(response: Any) -> Any:
        try:
            raw = response.read()
        except READ_ERRORS as exc:
            raise MurfError(f"failed to read API response: {exc}") from exc
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            snippet = raw.decode("utf-8", errors="replace").strip()[:300]
            raise MurfError(
                "API returned a non-JSON response", detail=snippet or None
            ) from exc

    # -- endpoints ---------------------------------------------------------

    def voices(self, *, page_size: int = 200, max_pages: int = 50) -> list[Mapping[str, Any]]:
        collected: list[Mapping[str, Any]] = []
        page = 1

        while page <= max_pages:
            response = self._request(
                "GET",
                API_VOICES,
                params={"pageNumber": page, "pageSize": page_size},
            )
            with response:
                payload = self._json(response)

            if isinstance(payload, Mapping):
                batch = payload.get("voices")
                total = payload.get("totalCount")
            else:
                batch = payload
                total = None

            if not isinstance(batch, list):
                raise MurfError("unexpected voices payload", detail=str(payload)[:300])

            collected.extend(item for item in batch if isinstance(item, Mapping))

            if len(batch) < page_size:
                break
            if isinstance(total, int) and len(collected) >= total:
                break
            page += 1

        return collected

    def generate(self, request: SpeechRequest) -> SpeechResult:
        response = self._request("POST", API_GENERATE, json_body=request.to_payload())
        with response:
            payload = self._json(response)
        if not isinstance(payload, Mapping):
            raise MurfError("unexpected generation payload", detail=str(payload)[:300])
        return SpeechResult(payload=payload)

    def stream(self, request: SpeechRequest, destination: Path) -> int:
        response = self._request("POST", API_STREAM, json_body=request.to_payload())
        with response:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                try:
                    detail = _error_detail(response.read())
                except READ_ERRORS:
                    detail = None
                raise MurfError(
                    "stream endpoint returned JSON instead of audio", detail=detail
                )
            try:
                return _atomic_stream(_iter_chunks(response), destination)
            except READ_ERRORS as exc:
                raise MurfError(f"audio stream failed: {exc}") from exc

    def download(self, url: str, destination: Path) -> int:
        response = self._request("GET", url, authorized=False)
        with response:
            try:
                return _atomic_stream(_iter_chunks(response), destination)
            except READ_ERRORS as exc:
                raise MurfError(f"audio download failed: {exc}") from exc


# -- helpers ---------------------------------------------------------------


def _error_detail(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except ValueError:
        return text[:500]

    if isinstance(payload, Mapping):
        for key in ("message", "detail", "error", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        try:
            return json.dumps(payload, ensure_ascii=False)[:500]
        except (TypeError, ValueError):
            return str(payload)[:500]
    return str(payload)[:500]


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _iter_chunks(response: Any, size: int = DOWNLOAD_CHUNK) -> Iterator[bytes]:
    while True:
        chunk = response.read(size)
        if not chunk:
            return
        yield chunk


def _atomic_stream(chunks: Iterable[bytes], destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".part")
    written = 0
    try:
        with temp.open("wb") as handle:
            for chunk in chunks:
                if not chunk:
                    continue
                handle.write(chunk)
                written += len(chunk)
        os.replace(temp, destination)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return written


def _atomic_bytes(data: bytes, destination: Path) -> int:
    return _atomic_stream((data,), destination)


def _strip_data_uri(value: str) -> str:
    if value.startswith("data:") and "," in value:
        return value.split(",", 1)[1]
    return value


def _encoded_audio(payload: Mapping[str, Any]) -> str | None:
    for key in ("encodedAudio", "audioContent", "audioBase64"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def save_result(client: MurfClient, result: SpeechResult, destination: Path) -> tuple[Path, int]:
    encoded = _encoded_audio(result.payload)
    if encoded:
        try:
            data = base64.b64decode(_strip_data_uri(encoded), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MurfError(f"invalid base64 audio in response: {exc}") from exc
        return destination, _atomic_bytes(data, destination)

    audio_file = result.payload.get("audioFile")
    if isinstance(audio_file, str) and audio_file:
        return destination, client.download(audio_file, destination)

    raise MurfError(
        "response contained neither audioFile nor encoded audio",
        detail=str(result.payload)[:300],
    )


def _slugify(text: str, *, limit: int = SLUG_LIMIT) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:limit].rstrip("-") or "speech")


def _resolve_output(output: str | None, text: str, audio_format: str) -> Path:
    extension = FORMAT_EXTENSIONS[audio_format]
    if not output:
        return Path.cwd() / f"{_slugify(text)}{extension}"

    path = Path(output).expanduser()
    if path.is_dir() or output.endswith(("/", "\\")):
        return path / f"{_slugify(text)}{extension}"
    if path.suffix:
        return path
    return path.with_suffix(extension)


def _clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 3].rstrip() + "..."


def _render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(row: Sequence[str]) -> str:
        cells = [cell.ljust(widths[index]) for index, cell in enumerate(row)]
        return "  ".join(cells).rstrip()

    lines = [render(headers), "  ".join("-" * width for width in widths)]
    lines.extend(render(row) for row in rows)
    return "\n".join(lines)


def _voice_value(voice: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = voice.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _voice_styles(voice: Mapping[str, Any]) -> str:
    styles = voice.get("styles")
    if isinstance(styles, list):
        parts = [str(item).strip() for item in styles if str(item).strip()]
        if parts:
            return ", ".join(parts)
    style = voice.get("style")
    if isinstance(style, str):
        return style.strip()
    return ""


def _read_source(text: str | None, file: str | None) -> str:
    if text and file:
        raise MurfError("provide either TEXT or --file, not both")

    if file:
        if file == "-":
            return sys.stdin.read()
        try:
            return Path(file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise MurfError(f"cannot read {file}: {exc}") from exc

    if text == "-":
        return sys.stdin.read()
    if text:
        return text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise MurfError("provide TEXT, --file, or pipe text through stdin")


def _validate_text(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise MurfError("text is empty")
    if len(cleaned) > MAX_TEXT_CHARS:
        raise MurfError(
            f"text is {len(cleaned)} characters; Murf accepts at most {MAX_TEXT_CHARS} per request"
        )
    return cleaned


def _voice_id(args: argparse.Namespace) -> str:
    voice = args.voice or os.environ.get(ENV_VOICE_ID)
    if not voice:
        raise MurfError(
            f"no voice selected (pass --voice or set {ENV_VOICE_ID}); run 'voices' to list ids"
        )
    return voice


def _bounded(label: str, value: int | None, low: int, high: int) -> int | None:
    if value is not None and not low <= value <= high:
        raise MurfError(f"{label} must be between {low} and {high}")
    return value


def _non_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def _speech_request(args: argparse.Namespace, text: str, voice_id: str) -> SpeechRequest:
    return SpeechRequest(
        text=text,
        voice_id=voice_id,
        audio_format=args.audio_format,
        sample_rate=args.sample_rate,
        channel_type=args.channel,
        style=args.style,
        rate=_bounded("rate", args.rate, -50, 50),
        pitch=_bounded("pitch", args.pitch, -50, 50),
        variation=args.variation,
        encode_as_base64=getattr(args, "base64", False),
        multi_native_locale=args.multi_native_locale,
    )


def _metadata(
    destination: Path,
    written: int,
    request: SpeechRequest,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "path": str(destination),
        "bytes": written,
        "voiceId": request.voice_id,
        "format": request.audio_format,
        "sampleRate": request.sample_rate,
        "audioLengthInSeconds": payload.get("audioLengthInSeconds"),
        "remainingCharacterCount": payload.get("remainingCharacterCount"),
    }


def _info(message: str, quiet: bool) -> None:
    if not quiet:
        print(message, file=sys.stderr)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


# -- commands --------------------------------------------------------------


def cmd_voices(args: argparse.Namespace, client: MurfClient) -> int:
    voices = client.voices()

    locale = (args.locale or "").lower()
    gender = (args.gender or "").lower()
    style = (args.style or "").lower()

    def matches(voice: Mapping[str, Any]) -> bool:
        if locale:
            haystack = _voice_value(voice, "locale", "language").lower()
            if locale not in haystack:
                return False
        if gender and _voice_value(voice, "gender").lower() != gender:
            return False
        if style and style not in _voice_styles(voice).lower():
            return False
        return True

    voices = [voice for voice in voices if matches(voice)]
    if args.limit is not None:
        voices = voices[: args.limit]

    if args.json:
        json.dump(voices, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    if not voices:
        _info("no voices matched", args.quiet)
        return 0

    rows = [
        [
            _voice_value(voice, "voiceId", "id"),
            _clip(_voice_value(voice, "name", "displayName"), 26),
            _voice_value(voice, "gender"),
            _voice_value(voice, "locale", "language"),
            _clip(_voice_styles(voice), 40),
        ]
        for voice in voices
    ]
    print(_render_table(["VOICE ID", "NAME", "GENDER", "LOCALE", "STYLES"], rows))
    _info(f"{len(voices)} voice(s)", args.quiet)
    return 0


def cmd_speak(args: argparse.Namespace, client: MurfClient) -> int:
    text = _validate_text(_read_source(args.text, args.file))
    request = _speech_request(args, text, _voice_id(args))
    destination = _resolve_output(args.output, text, args.audio_format)
    payload: Mapping[str, Any] = {}

    if args.stream:
        written = client.stream(request, destination)
    else:
        result = client.generate(request)
        payload = result.payload
        destination, written = save_result(client, result, destination)

    if args.json:
        print(json.dumps(_metadata(destination, written, request, payload), ensure_ascii=False))
    else:
        print(destination)
        seconds = payload.get("audioLengthInSeconds")
        suffix = f", {seconds}s" if isinstance(seconds, (int, float)) else ""
        _info(f"wrote {written} bytes{suffix}", args.quiet)
    return 0


def _read_batch(path: str) -> list[str]:
    try:
        if path == "-":
            content = sys.stdin.read()
        else:
            content = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise MurfError(f"cannot read {path}: {exc}") from exc

    segments: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        segments.append(stripped)
    return segments


def cmd_batch(args: argparse.Namespace, client: MurfClient) -> int:
    voice_id = _voice_id(args)
    segments = _read_batch(args.input)
    if not segments:
        raise MurfError(f"{args.input} contains no text")

    output_dir = Path(args.output).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    extension = FORMAT_EXTENSIONS[args.audio_format]
    prefix = args.prefix
    records: list[dict[str, Any]] = []
    failures: list[tuple[int, str]] = []

    for index, raw in enumerate(segments, 1):
        try:
            text = _validate_text(raw)
        except MurfError as exc:
            failures.append((index, str(exc)))
            _info(f"[{index}/{len(segments)}] skipped: {exc}", args.quiet)
            if args.fail_fast:
                break
            continue

        request = _speech_request(args, text, voice_id)
        destination = output_dir / f"{prefix}-{index:03d}{extension}"
        _info(f"[{index}/{len(segments)}] {_clip(text, 60)}", args.quiet)
        try:
            result = client.generate(request)
            destination, written = save_result(client, result, destination)
            records.append(_metadata(destination, written, request, result.payload))
        except MurfError as exc:
            failures.append((index, str(exc)))
            _info(f"[{index}/{len(segments)}] failed: {exc}", args.quiet)
            if args.fail_fast:
                break

        if index < len(segments) and args.delay > 0:
            time.sleep(args.delay)

    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
    else:
        print(output_dir)
        _info(
            f"generated {len(records)} of {len(segments)} segment(s), {len(failures)} failure(s)",
            args.quiet,
        )

    return 1 if failures else 0


# -- cli -------------------------------------------------------------------


def _add_audio_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        dest="audio_format",
        type=str.upper,
        choices=AUDIO_FORMATS,
        default="MP3",
        help="audio container (default: MP3)",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        choices=SAMPLE_RATES,
        default=24000,
        help="sample rate in Hz (default: 24000)",
    )
    parser.add_argument(
        "--channel",
        type=str.upper,
        choices=CHANNEL_TYPES,
        default="MONO",
        help="channel layout (default: MONO)",
    )
    parser.add_argument("--style", default=None, help="voice style, e.g. Conversational")
    parser.add_argument("--rate", type=int, default=None, help="speaking rate, -50..50")
    parser.add_argument("--pitch", type=int, default=None, help="pitch shift, -50..50")
    parser.add_argument("--variation", type=int, default=None, help="voice variation")
    parser.add_argument(
        "--multi-native-locale",
        default=None,
        help="locale for mixed-language text",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="murf.py",
        description="Murf.ai text-to-speech client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python murf.py voices --locale en-US --gender female\n"
            "  python murf.py speak \"Hello there.\" --voice en-US-natalie -o hi.mp3\n"
            "  python murf.py speak --file script.txt --voice en-US-natalie --format WAV\n"
            "  python murf.py batch lines.txt --voice en-US-natalie -o out --prefix clip\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--api-key",
        default=os.environ.get(ENV_API_KEY),
        help=f"Murf API key (default: ${ENV_API_KEY})",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL),
        help=f"API base URL (default: ${ENV_BASE_URL} or {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--region",
        choices=sorted(REGION_HOSTS),
        default=None,
        help="regional Murf host; overrides --base-url",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="request timeout in seconds")
    parser.add_argument("--retries", type=int, default=4, help="attempts per request (default: 4)")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")

    voices = subparsers.add_parser("voices", help="list available voices")
    voices.add_argument("--locale", help="filter by locale fragment, e.g. en-US")
    voices.add_argument("--gender", help="filter by gender")
    voices.add_argument("--style", help="filter by style fragment")
    voices.add_argument("--limit", type=_non_negative, default=None, help="show at most N voices")
    voices.add_argument("--json", action="store_true", help="print raw JSON")
    voices.set_defaults(handler=cmd_voices)

    speak = subparsers.add_parser("speak", help="synthesize one piece of text")
    speak.add_argument("text", nargs="?", help="text to speak; use '-' for stdin")
    speak.add_argument("--file", help="read text from a UTF-8 file; use '-' for stdin")
    speak.add_argument(
        "-v",
        "--voice",
        default=None,
        help=f"voice id (default: ${ENV_VOICE_ID})",
    )
    speak.add_argument(
        "-o",
        "--output",
        default=None,
        help="output file, directory, or omitted for a slugged filename",
    )
    speak.add_argument(
        "--stream",
        action="store_true",
        help="use the streaming endpoint instead of generate + download",
    )
    speak.add_argument(
        "--base64",
        action="store_true",
        help="ask the API to return base64 audio (saved locally)",
    )
    speak.add_argument("--json", action="store_true", help="print generation metadata as JSON")
    _add_audio_options(speak)
    speak.set_defaults(handler=cmd_speak)

    batch = subparsers.add_parser("batch", help="synthesize one clip per non-empty line")
    batch.add_argument("input", help="text file with one segment per line; '-' for stdin")
    batch.add_argument(
        "-v",
        "--voice",
        default=None,
        help=f"voice id (default: ${ENV_VOICE_ID})",
    )
    batch.add_argument(
        "-o",
        "--output",
        default="out",
        help="output directory (default: out)",
    )
    batch.add_argument("--prefix", default="speech", help="filename prefix (default: speech)")
    batch.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="seconds between requests (default: 0.5)",
    )
    batch.add_argument("--fail-fast", action="store_true", help="stop on the first failure")
    batch.add_argument("--json", action="store_true", help="print metadata for all clips as JSON")
    _add_audio_options(batch)
    batch.set_defaults(handler=cmd_batch)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_stdio()

    if args.region:
        args.base_url = REGION_HOSTS[args.region]

    try:
        retry = RetryPolicy(
            attempts=max(1, min(args.retries, 10)),
            timeout=max(1.0, args.timeout),
        )
        with MurfClient(args.api_key, base_url=args.base_url, retry=retry) as client:
            return args.handler(args, client)
    except MurfError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.detail:
            print(f"detail: {exc.detail}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
