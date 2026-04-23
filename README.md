# SIGCHI PDF Bulk Downloader

Downloads papers, posters, journal items, demos, etc. from the ACM Digital Library
for any SIGCHI event (CHI, CSCW, UIST, …) using DOIs from the event's
`programs.sigchi.org` JSON export.

## TL;DR for another LLM

- Input: any program JSON from `https://programs.sigchi.org/<event>/<year>` (e.g. `programs/CHI_2026_program.json`, saved in this repo as a sample).
- Script: `download_pdfs.py <path-to-program.json>`.
- Mechanism: launches **real Google Chrome** via Playwright with `playwright-stealth` and a persistent profile, clears one Cloudflare challenge, then pulls PDFs via the shared cookie jar.
- Output: `pdfs/<program-stem>/<safe-title> [<doi-with-underscores>].pdf`. Logs → `<program-stem>.download.log`. Failures → `<program-stem>.failed.tsv`. Re-running skips already-downloaded files.
- Content types (`--types`) are read from the JSON's `contentTypes` list at runtime — any name in there (case-insensitive), or `all`, works.

## Why this is harder than it looks

ACM DL is behind Cloudflare. The following approaches **all fail** (don't waste time trying them again):

| Approach | Result |
|---|---|
| `requests` with browser UA | 403 Cloudflare challenge |
| `curl_cffi` (Chrome impersonation) | 403 |
| `cloudscraper` | 403 |
| Headless Playwright Chromium + stealth | Stuck on "Just a moment…" |
| Headful Playwright Chromium + stealth | Stuck on "Just a moment…" |
| `nodriver` (undetected-chromedriver successor) | CDP connect failure on macOS |

**What works:** Playwright `launch_persistent_context(channel="chrome", headless=False)` + `playwright-stealth` + `--disable-blink-features=AutomationControlled`. The key is using the installed `/Applications/Google Chrome.app`, not the bundled Chromium.

**Critical gotcha — how PDFs are fetched:** Cloudflare fingerprints Playwright's `APIRequestContext` (`ctx.request.get`) differently from real browser fetches and **blocks the PDF endpoint** even after the session is warm. The HTML abstract pages work fine via `ctx.request`, but `GET /doi/pdf/<doi>` returns CF challenge pages. The fix: run the fetch **inside the page's JS context** via `page.evaluate('fetch(...)')`, then base64 the `ArrayBuffer` back out. That uses Chrome's real TLS fingerprint and passes CF. This is what `FETCH_JS` in the script does — don't "simplify" it back to `ctx.request.get`.

## Requirements

- macOS (tested on Darwin 24.5, Apple Silicon). Should work on Linux with a real Chrome/Chromium.
- Google Chrome installed at `/Applications/Google Chrome.app` (any recent version).
- Python 3.11+.
- ~15 GB of disk for all 1702 papers (average ~3–8 MB each; some up to 30 MB).
- Network: a stable connection. Run gets rate-shaped if you go faster than ~1 req/sec.

## Setup

Dependencies are managed by [uv](https://docs.astral.sh/uv/) via `pyproject.toml`.

```bash
cd /path/to/CHI2026
uv sync                      # installs playwright + playwright-stealth
```

A `.envrc` with `layout_uv` is included — if you use `direnv`, run `direnv allow` once and the venv is auto-activated on `cd`. Otherwise prefix commands with `uv run` (examples below).

Nothing else. `requests`, `curl_cffi`, `cloudscraper`, `nodriver` are **not** needed — don't install them.

## Input data

Drop program JSONs into `programs/` (e.g. `programs/CHI_2026_program.json`, `programs/CSCW_2025_program.json`). Grab them from `https://programs.sigchi.org/<event>/<year>` via the JSON download button / Export endpoint. Structure (relevant fields only):

```jsonc
{
  "contents": [
    {
      "id": 214789,
      "typeId": 14694,           // 14694 = Paper, 14743 = Poster, …
      "title": "…",
      "addons": {
        "doi": { "url": "https://doi.org/10.1145/3772318.3790431" }
      }
    },
    …
  ],
  "contentTypes": [ { "id": 14694, "name": "Paper" }, … ]
}
```

Counts in the 2026 export:
- 2782 total content items
- 2721 have DOIs
- 1702 Papers, 807 Posters, 67 Workshops, 40 Demos, 36 Journals, 32 Meetups, 12 SRC, 8 Panels, 5 Awards, 4 Keynotes, 69 Workshops, etc.

## Usage

```bash
# Default: just Papers from the given program
uv run download_pdfs.py programs/CHI_2026_program.json

# Specific types (comma-separated; names come from the JSON's contentTypes)
uv run download_pdfs.py programs/CHI_2026_program.json --types=paper,poster,journal

# Everything with a DOI
uv run download_pdfs.py programs/CHI_2026_program.json --types=all

# Smoke test
uv run download_pdfs.py programs/CHI_2026_program.json --types=paper --limit=3

# Tune politeness (default 1.5s between downloads)
uv run download_pdfs.py programs/CHI_2026_program.json --delay=3

# Custom output dir
uv run download_pdfs.py programs/CHI_2026_program.json --out=/some/other/dir

# Other SIGCHI events work the same way
uv run download_pdfs.py programs/CSCW_2025_program.json --types=paper,poster
```

With `direnv` active you can drop the `uv run` prefix and just run `python download_pdfs.py …`.

Valid `--types` values: `paper, poster, journal, demo, panel, keynote, award, workshop, course, meetup, src, mentoring, plaza, global, event, break, all`.

### Running in background

`download.log` is append-only, so you can `nohup` or `tmux` and come back:

```bash
nohup uv run download_pdfs.py programs/CHI_2026_program.json --types=paper > run.out 2>&1 &
tail -f CHI_2026_program.download.log
```

Re-running the same command after an interrupt **resumes** — existing PDFs in the output dir are skipped.

## How it works

1. **Load entries**: parse `programs/CHI_2026_program.json`, filter by `typeId`, extract DOI from `addons.doi.url` via regex `10\.\d{4,9}/\S+`.
2. **Launch Chrome**: `launch_persistent_context` with `channel="chrome"` so Cloudflare sees a real Chrome fingerprint. Profile lives in `.chrome-profile/` (cf_clearance persists there between runs, but not long enough to matter — CF re-issues it quickly).
3. **Clear Cloudflare**: navigate to `https://dl.acm.org/doi/<first-doi>`, wait for the page title to stop being "Just a moment…" (usually 2–5 s).
4. **Download loop**: for each DOI, `ctx.request.get("https://dl.acm.org/doi/pdf/<doi>")`. This reuses the browser's cookie jar (including `cf_clearance`) but doesn't render, so it's fast.
5. **Validate**: confirm HTTP 200 and `body[:4] == b"%PDF"`. If not, re-navigate the landing page to re-clear CF and retry (up to 3 attempts).
6. **Write atomically**: write to `…pdf.part`, then rename.

## Output

```
pdfs/<program-stem>/
  Rethinking External Communication of Autonomous Vehicles_ … [10.1145_3772318.3790431].pdf
  …
<program-stem>.download.log    # one line per attempt, timestamped
<program-stem>.failed.tsv      # doi<TAB>title<TAB>last_error — re-runnable by filtering the JSON to these DOIs
.chrome-profile/               # Playwright's persistent Chrome profile (shared across events)
```

Filenames: title is sanitized (bad chars → `_`, truncated to 120 chars) and the DOI (with `/` → `_`) is appended in brackets as a stable ID.

## ⚠️ ACM IP-ban threshold

**Do not exceed `--concurrency=4`.** Empirically:

- `c=1, delay=2` → ran 171 papers, no issues.
- `c=6, delay=0.3` → downloaded ~600 more, then ACM served `HTTP 403 "ACM Error: IP blocked"` (title), distinct from Cloudflare's "Just a moment…" page. Block lifts in hours but can require emailing `[email protected]`.

The script now detects the IP-block page by string match and aborts the run instead of hammering. Defaults are `--concurrency=2 --delay=1.5` (~2–3 s/paper, ~90 min for all 1702 papers). If you need to go faster, use a CMU/institutional VPN — those IPs aren't rate-limited the same way.

## Troubleshooting

- **Everything returns 403 / "Just a moment…"**: Chrome isn't installed at `/Applications/Google Chrome.app`, or you're running headless. The script forces `headless=False` by default — don't pass `--headless`.
- **`Failed to connect to browser`**: This is the nodriver error. You're looking at the wrong branch of the git history, or someone re-introduced nodriver. The working path is Playwright-only.
- **Partial PDFs**: look for `*.pdf.part` files — these are interrupted writes. The script cleans these up; if one remains, delete it and rerun.
- **Rate-limited mid-run**: ACM starts returning HTML wrappers instead of PDFs. The script auto-re-navigates to clear CF. If it persists, increase `--delay` to 5+ and restart.
- **Python requests the package `requests`**: you're on an old version of the script. The current script only needs `playwright` + `playwright-stealth`.

## Why not Zotero?

Zotero can do this too (import DOIs → "Find Available PDFs"), but also routes through Cloudflare and is manual per-batch. This script is ~3× faster and scriptable. The user's original suggestion (bulk BibTeX export × 7 → Zotero) works but is more clicks.

## License / Ethics note

CHI proceedings are Gold Open Access on ACM DL since 2025 — PDFs are freely
redistributable under CC-BY. Run with a polite delay (default 2s).
