#!/usr/bin/env python3
"""Download all CHI 2026 PDFs from ACM DL via real Chrome (Cloudflare-aware).

One Chrome context clears Cloudflare once, then N workers fetch PDFs in
parallel sharing the cookie jar via Playwright's async APIRequestContext.

Usage: python download_pdfs.py --types=paper --concurrency=6 --delay=0.3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright
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
    seen, dedup = set(), []
    for e in out:
        if e["doi"] in seen:
            continue
        seen.add(e["doi"])
        dedup.append(e)
    return dedup


async def clear_cloudflare(page, landing_url: str, max_wait: int = 60) -> bool:
    await page.goto(landing_url, wait_until="domcontentloaded", timeout=60000)
    start = time.time()
    while time.time() - start < max_wait:
        t = await page.title() or ""
        if t and "Just a moment" not in t and "Cloudflare" not in t:
            return True
        await asyncio.sleep(1.0)
    return False


class Counter:
    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.ok = 0
        self.fail = 0
        self.skip = 0
        self.lock = asyncio.Lock()
        self.log_f = open(LOG_PATH, "a")
        self.fail_f = open(FAIL_PATH, "a")

    async def record(self, kind: str, line: str, fail_row: str | None = None):
        async with self.lock:
            self.done += 1
            if kind == "ok":
                self.ok += 1
            elif kind == "skip":
                self.skip += 1
            else:
                self.fail += 1
                if fail_row:
                    self.fail_f.write(fail_row + "\n")
                    self.fail_f.flush()
            stamp = time.strftime("%H:%M:%S")
            msg = f"[{stamp}] {self.done}/{self.total} ok={self.ok} skip={self.skip} fail={self.fail} | {line}"
            print(msg, flush=True)
            self.log_f.write(msg + "\n")
            self.log_f.flush()

    def close(self):
        self.log_f.close()
        self.fail_f.close()


async def download_one(ctx, page, page_lock, entry: dict, out_dir: Path,
                        counter: Counter, sem: asyncio.Semaphore,
                        delay: float, stop_flag: dict):
    async with sem:
        if stop_flag["stop"]:
            return
        fname = f"{safe_filename(entry['title'])} [{entry['doi'].replace('/', '_')}].pdf"
        path = out_dir / fname

        if path.exists() and path.stat().st_size > 1000:
            await counter.record("skip", f"skip: {fname}")
            return

        url = f"https://dl.acm.org/doi/pdf/{quote(entry['doi'], safe='/')}"
        last_err = ""
        for attempt in range(1, 4):
            try:
                r = await ctx.request.get(url, headers={
                    "Referer": f"https://dl.acm.org/doi/{entry['doi']}",
                    "Accept": "application/pdf,*/*",
                }, timeout=90000)
                body = await r.body()
                ct = r.headers.get("content-type", "")
                if r.status == 200 and body[:4] == b"%PDF":
                    tmp = path.with_suffix(".pdf.part")
                    tmp.write_bytes(body)
                    tmp.rename(path)
                    await counter.record("ok", f"ok: {fname} ({len(body)} bytes)")
                    if delay > 0:
                        await asyncio.sleep(delay)
                    return
                last_err = f"HTTP {r.status} ct={ct} len={len(body)}"
                # Cloudflare re-challenge → one worker re-clears for everyone
                if r.status in (403, 429, 503) or body[:200].find(b"Just a moment") >= 0:
                    async with page_lock:
                        # Double-check we still need to clear (another worker may have done it)
                        cur_title = await page.title() if page.url != "about:blank" else ""
                        if "Just a moment" in cur_title or attempt > 1:
                            await clear_cloudflare(page, f"https://dl.acm.org/doi/{entry['doi']}")
                    await asyncio.sleep(1.0 * attempt)
            except Exception as ex:
                last_err = f"{type(ex).__name__}: {ex}"
                await asyncio.sleep(1.5 * attempt)

        await counter.record(
            "fail",
            f"FAIL {entry['doi']}: {last_err}",
            fail_row=f"{entry['doi']}\t{entry['title']}\t{last_err}",
        )


async def main_async(args):
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
    print(f"Loaded {len(entries)} entries (types={args.types}) concurrency={args.concurrency}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(exist_ok=True)

    counter = Counter(len(entries))
    stop_flag = {"stop": False}

    loop = asyncio.get_event_loop()
    def _sigint(*_):
        stop_flag["stop"] = True
        print("\n[SIGINT] stopping after in-flight downloads…", flush=True)
    loop.add_signal_handler(signal.SIGINT, _sigint)

    async with Stealth().use_async(async_playwright()) as p:
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chrome",
            headless=args.headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page_lock = asyncio.Lock()

        print(f"[init] clearing Cloudflare on {entries[0]['doi']} …")
        if not await clear_cloudflare(page, f"https://dl.acm.org/doi/{entries[0]['doi']}"):
            print("WARNING: Cloudflare not cleared within timeout; continuing anyway")

        sem = asyncio.Semaphore(args.concurrency)
        tasks = [
            asyncio.create_task(download_one(ctx, page, page_lock, e, out_dir,
                                              counter, sem, args.delay, stop_flag))
            for e in entries
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            counter.close()
            await ctx.close()

    print(f"\nDone. ok={counter.ok} fail={counter.fail} skip={counter.skip} out={out_dir}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", default="paper",
                    help="comma list: paper,poster,journal,demo,panel,keynote,award,workshop,course,all")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="parallel in-flight downloads (default 6)")
    ap.add_argument("--delay", type=float, default=0.3,
                    help="per-worker delay after each success (seconds)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--headless", action="store_true",
                    help="try headless (usually blocked by CF; default headful)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
