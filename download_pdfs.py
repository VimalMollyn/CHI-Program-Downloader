#!/usr/bin/env python3
"""Download all CHI 2026 PDFs from ACM DL via real Chrome (Cloudflare-aware).

Usage: python download_pdfs.py --types=paper,poster --delay=2

Requires: playwright, playwright_stealth. Uses your installed Chrome.app.
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

ROOT = Path(__file__).resolve().parent
JSON_PATH = ROOT / "CHI_2026_program.json"
OUT_DIR = ROOT / "pdfs"
LOG_PATH = ROOT / "download.log"
FAIL_PATH = ROOT / "failed.tsv"
PROFILE_DIR = ROOT / ".chrome-profile"

TYPE_NAMES = {
    14689: "Course", 14692: "Event", 14694: "Paper", 14697: "Workshop",
    14698: "Break", 14739: "Journal", 14740: "Demo", 14741: "Keynote",
    14742: "Meetup", 14743: "Poster", 14744: "Panel", 14746: "SRC",
    14805: "Award", 14839: "Mentoring", 14889: "Plaza", 14914: "Global",
}


def safe_filename(s: str, max_len: int = 120) -> str:
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", s).strip().rstrip(".")
    s = re.sub(r"\s+", " ", s)
    return s[:max_len] if len(s) > max_len else s


def load_entries(types: set[int] | None) -> list[dict]:
    data = json.loads(JSON_PATH.read_text())
    out = []
    for c in data.get("contents", []):
        if types is not None and c.get("typeId") not in types:
            continue
        doi_addon = c.get("addons", {}).get("doi") or {}
        url = doi_addon.get("url", "")
        m = re.search(r"(10\.\d{4,9}/\S+)$", url)
        if not m:
            continue
        doi = m.group(1).rstrip("/")
        out.append({
            "id": c["id"],
            "doi": doi,
            "title": c.get("title", ""),
            "type": TYPE_NAMES.get(c.get("typeId"), str(c.get("typeId"))),
        })
    # Dedup by doi
    seen, dedup = set(), []
    for e in out:
        if e["doi"] in seen:
            continue
        seen.add(e["doi"])
        dedup.append(e)
    return dedup


def clear_cloudflare(page, landing_url: str, max_wait: int = 60) -> bool:
    """Navigate to a DOI landing page and wait for Cloudflare challenge to clear."""
    page.goto(landing_url, wait_until="domcontentloaded", timeout=60000)
    start = time.time()
    while time.time() - start < max_wait:
        t = page.title() or ""
        if t and "Just a moment" not in t and "Cloudflare" not in t:
            return True
        time.sleep(1.0)
    return False


def fetch_pdf(ctx, doi: str) -> tuple[int, bytes, str]:
    url = f"https://dl.acm.org/doi/pdf/{quote(doi, safe='/')}"
    r = ctx.request.get(url, headers={
        "Referer": f"https://dl.acm.org/doi/{doi}",
        "Accept": "application/pdf,*/*",
    })
    body = r.body()
    return r.status, body, r.headers.get("content-type", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", default="paper",
                    help="comma list: paper,poster,journal,demo,panel,keynote,award,workshop,course,all")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between downloads")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--headless", action="store_true",
                    help="try headless (often blocked by CF; default headful)")
    args = ap.parse_args()

    name_to_id = {v.lower(): k for k, v in TYPE_NAMES.items()}
    if args.types.strip().lower() == "all":
        types = None
    else:
        wanted = [t.strip().lower() for t in args.types.split(",") if t.strip()]
        try:
            types = {name_to_id[w] for w in wanted}
        except KeyError as e:
            print(f"Unknown type: {e}. Valid: {sorted(name_to_id)} or 'all'", file=sys.stderr)
            sys.exit(2)

    entries = load_entries(types)
    if args.limit:
        entries = entries[: args.limit]
    print(f"Loaded {len(entries)} entries (types={args.types})")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(exist_ok=True)

    stop = {"flag": False}
    def _sigint(*_):
        stop["flag"] = True
        print("\n[SIGINT] finishing current download then exiting…")
    signal.signal(signal.SIGINT, _sigint)

    ok = fail = skipped = 0
    with Stealth().use_sync(sync_playwright()) as p, \
            open(LOG_PATH, "a") as log, open(FAIL_PATH, "a") as fail_f:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chrome",
            headless=args.headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Establish cf_clearance with first DOI
        first_doi = entries[0]["doi"]
        print(f"[init] clearing Cloudflare on {first_doi} …")
        if not clear_cloudflare(page, f"https://dl.acm.org/doi/{first_doi}"):
            print("WARNING: Cloudflare not cleared within timeout; continuing anyway")

        for i, e in enumerate(entries, 1):
            if stop["flag"]:
                break
            fname = f"{safe_filename(e['title'])} [{e['doi'].replace('/', '_')}].pdf"
            path = out_dir / fname
            stamp = time.strftime("%H:%M:%S")

            if path.exists() and path.stat().st_size > 1000:
                skipped += 1
                line = f"[{stamp}] {i}/{len(entries)} skip: {fname}"
                print(line); log.write(line + "\n"); log.flush()
                continue

            success = False
            last_err = ""
            for attempt in range(1, 4):
                try:
                    status, body, ct = fetch_pdf(ctx, e["doi"])
                    if status == 200 and body[:4] == b"%PDF":
                        tmp = path.with_suffix(".pdf.part")
                        tmp.write_bytes(body)
                        tmp.rename(path)
                        success = True
                        last_err = f"ok {len(body)} bytes"
                        break
                    last_err = f"HTTP {status} ct={ct} len={len(body)}"
                    # Likely re-challenged by CF → re-navigate landing page
                    if status in (403, 429, 503) or b"Just a moment" in body[:200]:
                        print(f"  [retry {attempt}] re-clearing Cloudflare…")
                        clear_cloudflare(page, f"https://dl.acm.org/doi/{e['doi']}")
                        time.sleep(2 * attempt)
                except Exception as ex:
                    last_err = f"{type(ex).__name__}: {ex}"
                    time.sleep(2 * attempt)

            if success:
                ok += 1
                line = f"[{stamp}] {i}/{len(entries)} ok: {fname}"
            else:
                fail += 1
                fail_f.write(f"{e['doi']}\t{e['title']}\t{last_err}\n"); fail_f.flush()
                line = f"[{stamp}] {i}/{len(entries)} FAIL {e['doi']}: {last_err}"
            print(line); log.write(line + "\n"); log.flush()
            time.sleep(args.delay)

        ctx.close()

    print(f"\nDone. ok={ok} fail={fail} skip={skipped} out={out_dir}")


if __name__ == "__main__":
    main()
