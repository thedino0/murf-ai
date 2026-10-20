"""Mock-server checks for the keyless client.

Run from the repository root:

    py -3 tests/test_free_client.py

The checks stay local: no Murf requests, no API key, no network dependency.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import murf  # noqa: E402

AUDIO = b"ID3\x04\x00\x00\x00\x00\x00\x00MOCK" + bytes(range(64))
STATE = {"audio_calls": 0, "retry_texts": [], "voices_calls": 0, "last_query": {}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def send_body(self, status, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/Prod/ping/common-voices":
            STATE["voices_calls"] += 1
            self.send_body(
                200,
                {
                    "responseCode": "SUCCESS",
                    "voiceGroups": [
                        {
                            "language": "English",
                            "locale": "en_US",
                            "accent": "US & Canada",
                            "voices": [
                                {
                                    "voiceId": "VM000000000001",
                                    "apiName": "en-US-test",
                                    "voiceName": "Test (F)",
                                    "gender": "Female",
                                    "voiceStyles": ["Conversational"],
                                },
                                {
                                    "voiceId": "VM000000000002",
                                    "apiName": "en-US-other",
                                    "voiceName": "Other (M)",
                                    "gender": "Male",
                                    "voiceStyles": [],
                                },
                                {
                                    "voiceId": "V000000000003",
                                    "apiName": "en-US-premium",
                                    "voiceName": "Premium (F)",
                                    "gender": "Female",
                                    "voiceStyles": ["General", "cheerful"],
                                    "voicePlanType": "BASIC",
                                },
                            ],
                        }
                    ],
                },
            )
        elif parsed.path == "/Prod/anonymous-tts/audio":
            STATE["audio_calls"] += 1
            STATE["last_query"] = {key: values[0] for key, values in query.items()}
            text = (query.get("text") or [""])[0]
            voice = (query.get("voiceId") or [""])[0]
            if len(text) > 500:
                self.send_body(502, {"message": "Invalid text provided"})
            elif "limit" in text.casefold():
                self.send_body(502, {"message": "Exceeded limit of calls"})
            elif voice == "VM000000000404":
                self.send_body(502, {"message": "Voice Not Found"})
            elif "retry" in text.casefold() and text not in STATE["retry_texts"]:
                STATE["retry_texts"].append(text)
                self.send_body(429, {"message": "slow down"}, headers={"Retry-After": "0"})
            else:
                self.send_body(200, AUDIO, "audio/mpeg")
        else:
            self.send_body(404, {"message": "not found"})


def expect_error(fn, needle):
    try:
        fn()
    except murf.MurfError as exc:
        return needle in str(exc) or needle in (exc.detail or "")
    raise AssertionError("expected MurfError")


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    client = murf.MurfClient(
        base_url=base,
        retry=murf.RetryPolicy(attempts=3, timeout=5, backoff=0.01),
    )

    try:
        voices = client.voices()
        assert len(voices) == 3, [v.api_name for v in voices]
        assert {v.api_name for v in voices} == {
            "en-US-test",
            "en-US-other",
            "en-US-premium",
        }
        assert client.voices() is voices, "voice list not cached"
        assert STATE["voices_calls"] == 1

        free = client.resolve_voice("en-US-test")
        premium = client.resolve_voice("premium")
        assert free.voice_id == "VM000000000001" and free.tier == "FREE"
        assert premium.voice_id == "V000000000003" and premium.tier == "PREMIUM"
        assert client.resolve_voice("test").voice_id == "VM000000000001"
        assert client.resolve_voice("VM000000000002").voice_id == "VM000000000002"
        assert client.resolve_voice("V000000000003").tier == "PREMIUM"
        assert expect_error(lambda: client.resolve_voice("nope"), "no voice")

        before = STATE["audio_calls"]
        audio = client.synthesize("retry this", "VM000000000001")
        assert audio == AUDIO
        assert STATE["audio_calls"] == before + 2, STATE["audio_calls"]

        before = STATE["audio_calls"]
        audio = client.synthesize(
            "controls",
            premium.voice_id,
            "cheerful",
            rate=10,
            pitch=-5,
            locale="fr_FR",
        )
        assert audio == AUDIO
        assert STATE["audio_calls"] == before + 1
        assert STATE["last_query"]["rate"] == "10"
        assert STATE["last_query"]["pitch"] == "-5"
        assert STATE["last_query"]["multiNativeLocale"] == "fr-FR"
        assert STATE["last_query"]["style"] == "cheerful"

        before = STATE["audio_calls"]
        assert expect_error(
            lambda: client.synthesize("bad rate", premium.voice_id, rate=51),
            "rate must be",
        )
        assert expect_error(
            lambda: client.synthesize("bad pitch", premium.voice_id, pitch=-51),
            "pitch must be",
        )
        assert STATE["audio_calls"] == before, "invalid controls hit the network"

        before = STATE["audio_calls"]
        assert expect_error(
            lambda: client.synthesize("x" * 501, "VM000000000001"),
            "Invalid text",
        )
        assert STATE["audio_calls"] == before + 1, "validation 502 was retried"

        before = STATE["audio_calls"]
        assert expect_error(
            lambda: client.synthesize("hello", "VM000000000404"),
            "Voice Not Found",
        )
        assert STATE["audio_calls"] == before + 1, "voice 502 was retried"

        before = STATE["audio_calls"]
        try:
            client.synthesize("limit this", premium.voice_id)
        except murf.MurfLimitError as exc:
            assert "call budget" in str(exc)
        else:
            raise AssertionError("expected MurfLimitError")
        assert STATE["audio_calls"] == before + 1, "limit 502 was retried"

        out = Path(__file__).with_name("retry_mock.mp3")
        out.unlink(missing_ok=True)
        written, chunks = murf._atomic_generate(
            client,
            "one. two. three.",
            premium,
            "General",
            out,
            delay=0,
            quiet=True,
            label="",
            rate=5,
            pitch=5,
            locale="en_US",
        )
        assert chunks == 1 and out.read_bytes() == AUDIO and written == len(AUDIO)
        assert STATE["last_query"]["rate"] == "5"
        assert STATE["last_query"]["pitch"] == "5"
        assert STATE["last_query"]["multiNativeLocale"] == "en-US"
        out.unlink()

        chunks = murf.split_text("word " * 400)
        assert all(murf._cost(chunk) <= 500 for chunk in chunks)
        assert len(chunks) >= 4
        print("all mock checks passed")
        return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
