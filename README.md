# murf-ai

Python clients for Murf.ai text-to-speech.

- `murf.py` uses the public endpoint behind Murf's no-signup TTS widget. No
  account, API key, cookie, or third-party package is required.
- `murf_api.py` uses Murf's documented REST API and requires an API key.

The two clients are intentionally separate. The keyless path is for zero-setup
use and the premium voices/controls that the public endpoint happens to honor.
The official path is for account quotas, regional hosts, streaming, and output
formats other than MP3.

## Requirements

Python 3.10 or newer. Both clients use only the standard library.

On Windows, use `py -3` anywhere the examples show `python`.

## Quick start

```powershell
py -3 murf.py voices --limit 10
py -3 murf.py speak "Hello from the couch." -o hello.mp3
py -3 murf.py speak --file script.txt --voice en-US-hannah
py -3 murf.py batch lines.txt -o out --prefix clip
```

`speak` accepts text as an argument, from `--file`, or from stdin with `-`.
If `-o` is omitted, the output name is derived from the first words of the
text. Directory outputs and explicit `.mp3` paths are both supported.

## Voice catalog

The public catalog currently returns 219 voices across 20 locales:

- 162 `VM...` voices are the free widget set.
- 57 `V...` voices are present in the catalog but not exposed by the widget.
  This client labels those `PREMIUM`.

```powershell
py -3 murf.py voices --tier premium
py -3 murf.py voices --tier premium --locale ja-JP --gender female
py -3 murf.py voices --search hannah --json
```

`--voice` accepts a catalog alias such as `en-US-hannah`, a full voice id such
as `V016093953653282U4`, or an unambiguous display name. The default voice is
`en-US-natalie`; set `MURF_VOICE` to change it.

## Repository layout

```text
murf.py            keyless public-endpoint client
murf_api.py        official API-key client
tests/             local mock-server checks
```

Audio files, partial downloads, virtualenvs, caches, and local env files are
ignored by git.

## Premium controls

The widget hides these controls, but the anonymous endpoint honors them. The
client validates the ranges before making a request.

| Option | Values | Behavior |
| --- | --- | --- |
| `--rate` | `-50` to `50` | Speech speed; `0` is the default, positive is faster |
| `--pitch` | `-50` to `50` | Pitch shift; `0` is the default |
| `--style` | style name | Per-voice delivery; use a name listed by `voices` |
| `--locale` | `en-US`, `fr-FR`, ... | Multilingual override; sent as `multiNativeLocale` |
| `[pause 1s]` | `0.1` to `5` seconds | Inline pause markup inside the text |

`--locale` is not sent as `locale`. The endpoint ignores the newer `locale`
parameter and honors the older `multiNativeLocale` field. Underscores are
normalized to hyphens.

```powershell
py -3 murf.py speak "Wait [pause 2s] for it." `
  --voice en-US-hannah --rate 10 --pitch -5 --locale en-US -o pause.mp3

py -3 murf.py batch lines.txt -o out `
  --voice de-DE-max --style cheerful --rate -10
```

Verified live before the endpoint's call budget was exhausted: `rate=50`
shortened a 4.885 s clip to 4.049 s, `rate=-50` stretched it to 9.064 s,
`pitch` changed the waveform without changing the request length, pause tags
added silence, and premium `V...` ids returned audio.

## Long text and limits

The public endpoint accepts at most 500 characters per request. Longer input is
split on sentence boundaries; each chunk costs one call. `--delay` controls the
pause between chunk requests, and output is assembled at MP3 frame level so
there are no inserted gaps.

A single request that is too long returns HTTP 502 `Invalid text provided`.
The client does not retry that payload.

The anonymous endpoint also has a per-IP call budget. In testing, a burst of
about 44 audio calls hit the limit and returned HTTP 502 `Exceeded limit of
calls` with no `Retry-After` header. The client raises `MurfLimitError` and a
batch stops instead of retrying an exhausted budget. Wait for the window to
reset or use the keyed client for guaranteed quota.

## Supported and not supported

| Capability | Keyless `murf.py` |
| --- | --- |
| Free `VM...` voices | Yes |
| Premium `V...` voices | Yes, while the public endpoint accepts them |
| Rate, pitch, and per-voice style | Yes |
| Multilingual locale override | Yes, through `multiNativeLocale` |
| Inline pauses | Yes, `[pause 1s]` |
| MP3 output | Yes, 44.1 kHz |
| Long text | Yes, chunked at 500 characters per request |
| Batch, stdin, JSON metadata | Yes |
| WAV, FLAC, OGG, ALAW, ULAW | No; format parameters are ignored |
| Custom sample rate or channel count | No; parameters are ignored |
| Base64 audio response | No; `encodeAsBase64` is ignored |
| Gen2 variation or model selection | No; `variation` and `modelVersion` are ignored |
| Word-level emphasis | Not exposed by the public endpoint |
| Pronunciation dictionary | Not wired into this client |
| Voice cloning, translation, dubbing, teams | No; account features |

Unknown parameters are not necessarily rejected. The server may silently drop
them, so the table reflects what was tested rather than what a flag name
suggests.

