#!/usr/bin/env python3
"""Download SIGCHI-event PDFs from ACM DL via real Chrome (Cloudflare-aware).

Works against any programs.sigchi.org JSON export (CHI, CSCW, UIST, …).
One Chrome context clears Cloudflare once, then N workers fetch PDFs in
parallel sharing the cookie jar via Playwright's async APIRequestContext.

Usage: python download_pdfs.py path/to/program.json --types=paper
"""
from __future__ import annotations

import argparse
import asyncio
import base64
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
PROFILE_DIR = ROOT / ".chrome-profile"


def safe_filename(s: str, max_len: int = 120) -> str:
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", s).strip().rstrip(".")
    s = re.sub(r"\s+", " ", s)
    return s[:max_len] if len(s) > max_len else s


def load_program(json_path: Path) -> tuple[dict, dict[int, str]]:
    data = json.loads(json_path.read_text())
    type_names = {t["id"]: t.get("name", str(t["id"])) for t in data.get("contentTypes", [])}
    return data, type_names


def load_entries(data: dict, type_names: dict[int, str],
                 types: set[int] | None) -> list[dict]:
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
            "type": type_names.get(c.get("typeId"), str(c.get("typeId"))),
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
    def __init__(self, total: int, log_path: Path, fail_path: Path):
        self.total = total
        self.done = 0
        self.ok = 0
        self.fail = 0
        self.skip = 0
        self.lock = asyncio.Lock()
        self.log_f = open(log_path, "a")
        self.fail_f = open(fail_path, "a")

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


FETCH_JS = """async (url) => {
    const r = await fetch(url, {credentials: 'include'});
    const buf = new Uint8Array(await r.arrayBuffer());
    let s = '';
    const CHUNK = 0x8000;
    for (let i = 0; i < buf.length; i += CHUNK) {
        s += String.fromCharCode.apply(null, buf.subarray(i, i + CHUNK));
    }
    return {status: r.status, ct: r.headers.get('content-type') || '', b64: btoa(s)};
}"""


async def download_one(worker_page, page_lock, entry: dict, out_dir: Path,
                        counter: Counter, delay: float, stop_flag: dict):
    if stop_flag["stop"]:
        return
    fname = f"{safe_filename(entry['title'])} [{entry['doi'].replace('/', '_')}].pdf"
    path = out_dir / fname

    if path.exists() and path.stat().st_size > 1000:
        await counter.record("skip", f"skip: {fname}")
        return

    pdf_path = f"/doi/pdf/{quote(entry['doi'], safe='/')}"
    last_err = ""
    for attempt in range(1, 4):
        try:
            data = await worker_page.evaluate(FETCH_JS, pdf_path)
            body = base64.b64decode(data["b64"])
            ct = data["ct"]
            status = data["status"]
            if status == 200 and body[:4] == b"%PDF":
                tmp = path.with_suffix(".pdf.part")
                tmp.write_bytes(body)
                tmp.rename(path)
                await counter.record("ok", f"ok: {fname} ({len(body)} bytes)")
                if delay > 0:
                    await asyncio.sleep(delay)
                return
            last_err = f"HTTP {status} ct={ct} len={len(body)}"
            if b"IP blocked" in body[:40000] or b"Your IP Address has been blocked" in body[:40000]:
                stop_flag["stop"] = True
                last_err = "ACM IP BLOCKED — aborting run; wait hours or switch network/VPN"
                break
            if status in (403, 429, 503) or body[:200].find(b"Just a moment") >= 0:
                async with page_lock:
                    await worker_page.goto(f"https://dl.acm.org/doi/{entry['doi']}", timeout=60000)
                await asyncio.sleep(1.0 * attempt)
        except Exception as ex:
            last_err = f"{type(ex).__name__}: {ex}"
            await asyncio.sleep(1.5 * attempt)

    await counter.record(
        "fail",
        f"FAIL {entry['doi']}: {last_err}",
        fail_row=f"{entry['doi']}\t{entry['title']}\t{last_err}",
    )


async def worker_loop(pool_page, page_lock, queue: asyncio.Queue, out_dir,
                      counter, delay, stop_flag):
    while True:
        entry = await queue.get()
        try:
            if entry is None or stop_flag["stop"]:
                return
            await download_one(pool_page, page_lock, entry, out_dir,
                               counter, delay, stop_flag)
        finally:
            queue.task_done()


async def main_async(args):
    json_path = Path(args.program).expanduser().resolve()
    if not json_path.exists():
        print(f"Program JSON not found: {json_path}", file=sys.stderr)
        sys.exit(2)

    data, type_names = load_program(json_path)
    name_to_id = {v.lower(): k for k, v in type_names.items()}
    if args.types.strip().lower() == "all":
        types = None
    else:
        wanted = [t.strip().lower() for t in args.types.split(",") if t.strip()]
        try:
            types = {name_to_id[w] for w in wanted}
        except KeyError as e:
            print(f"Unknown type: {e}. Valid: {sorted(name_to_id)} or 'all'", file=sys.stderr)
            sys.exit(2)

    entries = load_entries(data, type_names, types)
    if args.limit:
        entries = entries[: args.limit]
    print(f"Loaded {len(entries)} entries from {json_path.name} "
          f"(types={args.types}) concurrency={args.concurrency}")
    if not entries:
        print("No entries matched — nothing to do.", file=sys.stderr)
        sys.exit(0)

    stem = json_path.stem
    out_dir = Path(args.out) if args.out else ROOT / "pdfs" / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.log) if args.log else ROOT / f"{stem}.download.log"
    fail_path = Path(args.failed) if args.failed else ROOT / f"{stem}.failed.tsv"
    PROFILE_DIR.mkdir(exist_ok=True)

    counter = Counter(len(entries), log_path, fail_path)
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
        first_page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page_lock = asyncio.Lock()

        print(f"[init] warming session on {entries[0]['doi']} …")
        await first_page.goto(f"https://dl.acm.org/doi/{entries[0]['doi']}", timeout=60000)
        # brief wait in case CF puts up challenge
        await clear_cloudflare(first_page, first_page.url)

        # One page per worker — each shares the context's cookie jar
        worker_pages = [first_page]
        for _ in range(args.concurrency - 1):
            wp = await ctx.new_page()
            await wp.goto(f"https://dl.acm.org/doi/{entries[0]['doi']}", timeout=60000)
            worker_pages.append(wp)

        queue: asyncio.Queue = asyncio.Queue()
        for e in entries:
            queue.put_nowait(e)
        for _ in range(args.concurrency):
            queue.put_nowait(None)  # sentinel per worker

        workers = [
            asyncio.create_task(worker_loop(wp, page_lock, queue, out_dir,
                                             counter, args.delay, stop_flag))
            for wp in worker_pages
        ]
        try:
            await asyncio.gather(*workers)
        finally:
            counter.close()
            await ctx.close()

    print(f"\nDone. ok={counter.ok} fail={counter.fail} skip={counter.skip} out={out_dir}")


def main():
    ap = argparse.ArgumentParser(
        description="Bulk-download PDFs for any SIGCHI event from ACM DL.")
    ap.add_argument("program", help="path to a programs.sigchi.org JSON export")
    ap.add_argument("--types", default="paper",
                    help="comma list of content-type names from the JSON "
                         "(e.g. paper,poster,journal,demo,panel,keynote), or 'all'")
    ap.add_argument("--concurrency", type=int, default=2,
                    help="parallel in-flight downloads (default 2 — ACM IP-bans at >4)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="per-worker delay after each success (seconds)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="",
                    help="output dir (default: ./pdfs/<program-stem>/)")
    ap.add_argument("--log", default="",
                    help="log file (default: ./<program-stem>.download.log)")
    ap.add_argument("--failed", default="",
                    help="failed TSV (default: ./<program-stem>.failed.tsv)")
    ap.add_argument("--headless", action="store_true",
                    help="try headless (usually blocked by CF; default headful)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
