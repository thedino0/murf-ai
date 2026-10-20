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
