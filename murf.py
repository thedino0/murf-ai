#!/usr/bin/env python3
"""Murf.ai text-to-speech without an API key.

Uses the public endpoint behind Murf's no-signup TTS widget:

    GET https://murf.ai/Prod/anonymous-tts/audio?text=...&voiceId=...&style=...

No account, no api-key header, no cookie. The public catalog contains 219
voices: 162 free widget voices (VM... ids, marked FREE) and 57 premium
voices (V... ids, marked PREMIUM). Both sets are accepted by the endpoint.

The endpoint also honors the studio's premium controls even though the widget
does not expose them:

    --rate -50..50       speech speed
    --pitch -50..50      voice pitch
    --style NAME         per-voice style, e.g. Conversational, cheerful
    --locale LOCALE      multilingual pronunciation override
    [pause 2s]           inline pause markup, 0.1-5 seconds

Each request accepts at most 500 characters, so longer text is split on
sentence boundaries and the MP3 chunks are joined frame-by-frame into one
file. The public endpoint has a per-IP call budget; a long script consumes
one call per 500 characters.

Examples:
    python murf.py voices --tier premium
    python murf.py speak "Hello from the couch." --voice en-US-hannah
    python murf.py speak "Wait [pause 2s] for it." --rate 10 --pitch -5
    python murf.py speak --file script.txt --voice en-US-natalie --locale en-US
    python murf.py batch lines.txt -o out --prefix clip

Standard library only. Python 3.10+.
"""

from __future__ import annotations

import argparse
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
from typing import Any, Mapping, Sequence

APP_NAME = "murf-free"
VERSION = "2.1.0"
USER_AGENT = f"{APP_NAME}/{VERSION}"

ENV_VOICE = "MURF_VOICE"
ENV_BASE_URL = "MURF_BASE_URL"

DEFAULT_BASE_URL = "https://murf.ai"
DEFAULT_VOICE = "en-US-natalie"

API_VOICES = "/Prod/ping/common-voices"
API_AUDIO = "/Prod/anonymous-tts/audio"

MAX_CHUNK_CHARS = 500
ADJUSTMENT_MIN = -50
ADJUSTMENT_MAX = 50
DIRECT_VOICE_ID = re.compile(r"V(?:M)?[A-Z0-9]{10,}")
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
READ_ERRORS = (OSError, http.client.HTTPException, ValueError)
SLUG_LIMIT = 60


class MurfError(RuntimeError):
    """Endpoint, network, or payload failure."""

    def __init__(self, message: str, *, status: int | None = None, detail: str | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class MurfLimitError(MurfError):
    """The public endpoint's per-IP call budget is exhausted."""


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    timeout: float = 90.0
    backoff: float = 0.8
    max_backoff: float = 15.0


@dataclass(frozen=True)
class Voice:
    voice_id: str
    api_name: str
    name: str
    gender: str
    locale: str
    language: str
    styles: tuple[str, ...]
    tier: str = "FREE"

    @property
    def label(self) -> str:
        return self.api_name or self.voice_id

    @property
    def is_premium(self) -> bool:
        return self.tier == "PREMIUM"


class MurfClient:
    """Keyless client for Murf's public TTS widget endpoint."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        retry: RetryPolicy | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._retry = retry or RetryPolicy()
        self._voices: list[Voice] | None = None

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str = "application/json",
    ) -> Any:
        target = self._base_url + path
        if params:
            separator = "&" if "?" in target else "?"
            target = f"{target}{separator}{urllib.parse.urlencode(params)}"

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Referer": self._base_url + "/text-to-speech",
        }
        last_error = "unknown error"

        for attempt in range(1, self._retry.attempts + 1):
            request = urllib.request.Request(target, method="GET", headers=headers)
            try:
                return urllib.request.urlopen(request, timeout=self._retry.timeout)
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    body = exc.read()
                except READ_ERRORS:
                    body = b""
                retry_after = _parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers else None
                )
                exc.close()

                message = _json_message(body)
                detail = message or body.decode("utf-8", errors="replace").strip()[:500] or None
                if message and "exceeded limit of calls" in message.casefold():
                    raise MurfLimitError(
                        "public endpoint call budget exhausted",
                        status=status,
                        detail=(
                            "Murf's anonymous TTS endpoint is rate limiting this IP. "
                            "Wait for the window to reset, reduce the number of chunks, "
                            "or use murf_api.py with a key."
                        ),
                    ) from exc
                application_error = status == 502 and bool(message)

                if (
                    status in RETRY_STATUS
                    and attempt < self._retry.attempts
                    and not application_error
                ):
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

    # -- catalog -----------------------------------------------------------

    def voices(self) -> list[Voice]:
        if self._voices is not None:
            return self._voices

        response = self._request(API_VOICES)
        with response:
            payload = self._json(response)

        groups = payload.get("voiceGroups") if isinstance(payload, Mapping) else None
        if not isinstance(groups, list):
            raise MurfError("unexpected voices payload", detail=str(payload)[:300])

        collected: dict[str, Voice] = {}
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            language = _text(group.get("language"))
            locale = _text(group.get("locale"))
            accent = _text(group.get("accent"))
            if not locale:
                locale = "-".join(part for part in (language, accent) if part)
            locale = locale.replace("_", "-")

            entries = group.get("voices")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                voice_id = _text(entry.get("voiceId"))
                if not voice_id.startswith("V"):
                    continue
                api_name = _text(entry.get("apiName")) or voice_id
                collected[voice_id] = Voice(
                    voice_id=voice_id,
                    api_name=api_name,
                    name=_text(entry.get("voiceName")) or _text(entry.get("userFriendlyName")) or api_name,
                    gender=_text(entry.get("gender")),
                    locale=locale,
                    language=language,
                    styles=_style_names(entry.get("voiceStyles")),
                    tier="FREE" if voice_id.startswith("VM") else "PREMIUM",
                )

        self._voices = sorted(
            collected.values(),
            key=lambda voice: (
                0 if not voice.is_premium else 1,
                voice.language.casefold(),
                voice.locale.casefold(),
                voice.name.casefold(),
            ),
        )
        return self._voices

    def resolve_voice(self, value: str) -> Voice:
        requested = value.strip()
        if not requested:
            raise MurfError("voice id is empty")

        direct_id = requested.upper()
        if DIRECT_VOICE_ID.fullmatch(direct_id):
            return Voice(
                voice_id=direct_id,
                api_name="",
                name=direct_id,
                gender="",
                locale="",
                language="",
                styles=(),
                tier="FREE" if direct_id.startswith("VM") else "PREMIUM",
            )

        voices = self.voices()
        key = requested.casefold()

        for voice in voices:
            if key in (voice.api_name.casefold(), voice.voice_id.casefold()):
                return voice
        for voice in voices:
            if key == _bare_name(voice.name).casefold():
                return voice

        matches = [
            voice
            for voice in voices
            if key in voice.api_name.casefold()
            or key in voice.name.casefold()
            or key in _bare_name(voice.name).casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise MurfError(
                f"no voice matched {requested!r}",
                detail="run 'voices' to list aliases",
            )

        candidates = ", ".join(voice.api_name for voice in matches[:8])
        raise MurfError(
            f"voice {requested!r} is ambiguous; try a full alias",
            detail=f"matches: {candidates}",
        )

    # -- synthesis ---------------------------------------------------------

    def synthesize(
        self,
        text: str,
        voice_id: str,
        style: str | None = None,
        *,
        rate: int = 0,
        pitch: int = 0,
        locale: str | None = None,
    ) -> bytes:
        _validate_adjustment("rate", rate)
        _validate_adjustment("pitch", pitch)

        params: dict[str, str] = {"text": text, "voiceId": voice_id}
        if style:
            params["style"] = style
        if rate:
            params["rate"] = str(rate)
        if pitch:
            params["pitch"] = str(pitch)
        if locale:
            params["multiNativeLocale"] = locale.strip().replace("_", "-")

        response = self._request(
            API_AUDIO,
            params=params,
            accept="audio/mpeg,audio/*;q=0.9,*/*;q=0.8",
        )
        with response:
            content_type = response.headers.get("Content-Type", "")
            try:
                audio = response.read()
            except READ_ERRORS as exc:
                raise MurfError(f"failed to read audio response: {exc}") from exc

        if not audio:
            raise MurfError("endpoint returned empty audio")
        looks_like_mp3 = audio.startswith(b"ID3") or (
            len(audio) > 1 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0
        )
        if not looks_like_mp3:
            detail = _json_message(audio) or audio.decode("utf-8", errors="replace")[:300]
            raise MurfError(
                "endpoint returned a non-audio response",
                detail=detail or f"content-type: {content_type or 'unknown'}",
            )
        return audio


# -- text splitting --------------------------------------------------------


def _cost(text: str) -> int:
    """UTF-16 code units, the strictest reading of Murf's 500-char limit."""
    return len(text.encode("utf-16-le")) // 2


def _hard_split(token: str, limit: int) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    size = 0
    for char in token:
        char_cost = _cost(char)
        if buffer and size + char_cost > limit:
            parts.append("".join(buffer))
            buffer = [char]
            size = char_cost
        else:
            buffer.append(char)
            size += char_cost
    if buffer:
        parts.append("".join(buffer))
    return parts


def _fit_sentence(sentence: str, limit: int) -> list[str]:
    if _cost(sentence) <= limit:
        return [sentence]

    pieces: list[str] = []
    for word in sentence.split(" "):
        if not word:
            continue
        if _cost(word) <= limit:
            pieces.append(word)
        else:
            pieces.extend(_hard_split(word, limit))
    return pieces


def split_text(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    normalized = " ".join(text.split())
    if not normalized:
        raise MurfError("text is empty")
    if _cost(normalized) <= limit:
        return [normalized]

    chunks: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?…])\s+", normalized):
        if not sentence:
            continue
        for piece in _fit_sentence(sentence, limit):
            if not current:
                current = piece
            elif _cost(current) + 1 + _cost(piece) <= limit:
                current = f"{current} {piece}"
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)

    for chunk in chunks:
        if _cost(chunk) > limit:
            raise MurfError("internal error: chunk exceeds endpoint limit")
    return chunks


# -- audio assembly --------------------------------------------------------


def strip_id3(data: bytes) -> bytes:
    """Remove ID3 tags so MP3 chunks can be concatenated at frame level."""
    if data[:3] == b"ID3" and len(data) >= 10:
        size = (
            ((data[6] & 0x7F) << 21)
            | ((data[7] & 0x7F) << 14)
            | ((data[8] & 0x7F) << 7)
            | (data[9] & 0x7F)
        )
        footer = 10 if data[5] & 0x10 else 0
        data = data[10 + size + footer :]
    if len(data) >= 128 and data[-128:-125] == b"TAG":
        data = data[:-128]
    return data


def _atomic_generate(
    client: MurfClient,
    text: str,
    voice: Voice,
    style: str | None,
    destination: Path,
    *,
    delay: float,
    quiet: bool,
    label: str,
    rate: int = 0,
    pitch: int = 0,
    locale: str | None = None,
) -> tuple[int, int]:
    chunks = split_text(text)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".part")
    written = 0

    try:
        with temp.open("wb") as handle:
            for index, chunk in enumerate(chunks, 1):
                _info(
                    f"{label}chunk {index}/{len(chunks)} ({_cost(chunk)} chars)",
                    quiet,
                )
                audio = client.synthesize(
                    chunk,
                    voice.voice_id,
                    style,
                    rate=rate,
                    pitch=pitch,
                    locale=locale,
                )
                payload = audio if index == 1 else strip_id3(audio)
                if not payload:
                    raise MurfError("endpoint returned empty audio frames")
                handle.write(payload)
                written += len(payload)
                if index < len(chunks) and delay > 0:
                    time.sleep(delay)
        os.replace(temp, destination)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise

    return written, len(chunks)


# -- helpers ---------------------------------------------------------------


def _json_message(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    for key in ("message", "detail", "error", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _validate_adjustment(name: str, value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not ADJUSTMENT_MIN <= value <= ADJUSTMENT_MAX
    ):
        raise MurfError(
            f"{name} must be an integer between {ADJUSTMENT_MIN} and {ADJUSTMENT_MAX}"
        )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _style_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    styles: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            styles.append(item.strip())
        elif isinstance(item, Mapping):
            name = _text(item.get("name")) or _text(item.get("style"))
            if name:
                styles.append(name)
    return tuple(dict.fromkeys(styles))


def _bare_name(name: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


def _pick_style(voice: Voice, requested: str | None) -> str | None:
    if requested:
        return requested
    lookup = {style.casefold(): style for style in voice.styles}
    for candidate in ("conversational", "narration"):
        if candidate in lookup:
            return lookup[candidate]
    return voice.styles[0] if voice.styles else None


def _voice_dict(voice: Voice) -> dict[str, Any]:
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


def _clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 3].rstrip() + "..."


def _slugify(text: str, *, limit: int = SLUG_LIMIT) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "speech"


def _resolve_output(output: str | None, text: str) -> Path:
    if not output:
        return Path.cwd() / f"{_slugify(text)}.mp3"

    path = Path(output).expanduser()
    if path.is_dir() or output.endswith(("/", "\\")):
        return path / f"{_slugify(text)}.mp3"
    if not path.suffix:
        return path.with_suffix(".mp3")
    if path.suffix.casefold() != ".mp3":
        raise MurfError("the free endpoint only produces MP3; use an .mp3 output path")
    return path


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


def _validate_text(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise MurfError("text is empty")
    return cleaned


def _voice_id(args: argparse.Namespace) -> str:
    return args.voice or os.environ.get(ENV_VOICE) or DEFAULT_VOICE


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


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def _adjustment(value: str) -> int:
    number = int(value)
    if not ADJUSTMENT_MIN <= number <= ADJUSTMENT_MAX:
        raise argparse.ArgumentTypeError(
            f"must be between {ADJUSTMENT_MIN} and {ADJUSTMENT_MAX}"
        )
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number
