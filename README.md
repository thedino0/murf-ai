# murf-ai

Python clients and a browser UI for Murf.ai text-to-speech.

- `murf.py` uses the public endpoint behind Murf's no-signup TTS widget. No
  account, API key, cookie, or third-party package is required.
- `murf_api.py` uses Murf's documented REST API and requires an API key.
- `app.py` serves a web app around `murf.py`, locally or on Vercel.

The two clients are intentionally separate. The keyless path is for zero-setup
use and the premium voices/controls that the public endpoint happens to honor.
The official path is for account quotas, regional hosts, streaming, and output
formats other than MP3.

## Requirements

Python 3.10 or newer. The clients and the web app use only the standard
library.

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

## Web app

`app.py` serves a single-page voice lab with searchable voice filters, premium
badges, style selection, rate and pitch sliders, locale override, pause
insertion, an audio player, and MP3 download. It uses the keyless `murf.py`
client, so there is no API key to configure. The HTML, CSS, JS, and favicon
live in `public/`; `api/` contains the Vercel Python functions.

Run it locally:

```powershell
py -3 app.py
# open http://127.0.0.1:10000
```

The server binds `0.0.0.0` and reads `PORT` from the environment. Set `HOST` or
pass `--host` / `--port` if the defaults do not fit.

### Deploy on Vercel

The repository includes `vercel.json`, Python functions in `api/`, and static
files in `public/`. No build step or third-party package is required.

1. Push the branch and open the repository in Vercel.
2. Choose **Add New → Project**, import the repository, and select the
   `feat/vercel` branch. Use `main` if this branch has been merged.
3. Leave the framework preset on **Other**. Use the repository root, leave the
   build command empty, and use `public` as the output directory. `vercel.json`
   also sets the output directory, function duration, health rewrite, and
   security headers.
4. Deploy. Vercel serves `public/` at the site root and builds each file in
   `api/` as a function at `/api/<name>`.
5. Open `/healthz` to verify the deployment, then load the page and generate a
   short clip.

The Hobby plan is enough for this site. The optional Vercel CLI route is:

```powershell
npx vercel --prod
```

Vercel's file-function runtime supports the `BaseHTTPRequestHandler`
subclasses in `api/`. `server/vercel_core.py` supplies the shared application
and maps each function URL onto the routes in `app.py`.

### Web environment knobs

| Variable | Default | Purpose |
| --- | --- | --- |
| `WEB_MAX_TEXT` | `5000` local, `2500` on Vercel | Maximum characters accepted by the web form |
| `WEB_RATE_CHUNKS` | `40` | Audio chunks allowed per client IP per window |
| `WEB_RATE_WINDOW` | `900` | Rate-limit window in seconds |
| `WEB_MAX_CONCURRENT` | `2` | Maximum simultaneous Murf requests per process |
| `WEB_CHUNK_DELAY` | `0.15` | Delay between chunk requests in seconds |
| `MURF_BASE_URL` | `https://murf.ai` | Override the endpoint origin, mainly for local mocks |
| `PORT` | `10000` | Local bind port; Vercel functions do not use it |

The rate limit counts 500-character chunks, not button clicks. A single
5,000-character script costs about 10 units. The host's outbound IP may be
shared with other services, and Murf budgets the public endpoint per IP, so a
busy deployment can still hit the upstream limit. The page reports that case
and suggests waiting or using the official client.

Vercel limits a function response to 4.5 MB. The default Vercel text cap of
2,500 characters is roughly five audio chunks and leaves headroom under that
limit. Raising `WEB_MAX_TEXT` also raises the chance of a truncated response;
the local server has no such cap.

The Vercel rate limiter is in memory per function instance. It slows a single
warm instance but is not a global quota; Vercel can start more instances and
Murf still budgets calls per outbound IP.

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
app.py             local web server and shared application
public/            HTML, CSS, JS, and favicon served by Vercel
api/               Vercel Python function routes
server/            Vercel adapter around app.py
vercel.json        static output, functions, rewrite, headers
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

## Official API client

Use `murf_api.py` when the keyless endpoint's MP3 output or per-IP budget is
not enough. It reads `MURF_API_KEY` from the environment; `--api-key` is also
accepted for one-off runs.

```powershell
$env:MURF_API_KEY = "..."
py -3 murf_api.py voices --limit 10
py -3 murf_api.py speak "Hello." --voice en-US-natalie `
  -o hello.wav --format WAV --sample-rate 44100
py -3 murf_api.py speak "Stream this." --voice en-US-natalie `
  --stream -o stream.mp3
py -3 murf_api.py batch lines.txt -o out --prefix clip --fail-fast
```

The official client supports MP3, WAV, FLAC, ALAW, and ULAW; 8, 16, 24, 44.1,
and 48 kHz; mono or stereo; streaming and optional base64 responses;
`--variation`; and `--multi-native-locale`. Account quotas apply. Use
`--region` or `--base-url` only when Murf gives you a regional host.

Keep the key in the environment, not in a tracked file.

## Tests

```powershell
py -3 tests/test_free_client.py
py -3 tests/test_web.py
```

The checks start local HTTP servers and exercise retries, catalog caching,
free and premium lookup, rate/pitch/locale forwarding, limit handling, chunk
sizing, the web API, and the HTML/static responses. They do not call Murf and
do not need a key.

## How it works

`murf.py` makes plain GET requests to two public endpoints:

- `/Prod/ping/common-voices` returns the same catalog used by the widget.
- `/Prod/anonymous-tts/audio` returns MP3 bytes for one text segment.

Requests are retried on 408, 425, 429, and transient 5xx responses. A 502
with a JSON `message` is treated as an application error and is not retried.
Responses must start with an MP3 frame or ID3 header; HTML error pages are
rejected instead of saved as audio.

The anonymous endpoint is not a documented API. It can change without notice,
and the premium catalog entries are not guaranteed to remain available. The
client keeps the endpoint paths and request fields in one place so they are
easy to update.

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

## Troubleshooting

| Error or symptom | What to do |
| --- | --- |
| `Exceeded limit of calls` | Wait for the per-IP window to reset, or switch to `murf_api.py` |
| `Invalid text provided` | Shorten the request; the server rejected it as over the limit |
| `Voice Not Found` | The premium id is no longer accepted; pick another voice |
| `endpoint returned a non-audio response` | The endpoint served HTML or JSON instead of MP3; retry later |
| Empty output directory | Check the batch input has non-empty lines and `-o` points to a writable path |

