#!/usr/bin/env python3
"""spine_fast.py — скоростной сборщик Spine-ассетов.

Работает с .codespace/cdp.py (Crawler). Требования: только браузер + сеть,
никаких знаний о конкретной площадке.
"""
from __future__ import annotations
import asyncio
import hashlib
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cdp import Crawler  # noqa: E402

OUT_DIR = Path("spine_out")
BUDGET_SEC = 16            # жёсткий лимит на одну игру
DOWNLOAD_WORKERS = 14
SPINE_RE = re.compile(r'\.(skel|json|atlas|png|webp|ktx2?|basis|data|bin|bundle)(\?|$)', re.I)
HINT_RE = re.compile(
    r'spine|skeleton|atlas|attachment|bone|slot|skin|symbol|reel|bonus|collect|frame', re.I)


def is_spine(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    if SPINE_RE.search(url):
        return True
    path = urlparse(url).path.lower()
    return bool(HINT_RE.search(url)) and any(
        path.endswith(ext) for ext in (".json", ".skel", ".atlas", ".png", ".webp"))


def game_name(url: str) -> str:
    p = urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    if "gameName=" in url:
        m = re.search(r"gameName=([a-zA-Z0-9_]+)", url)
        if m:
            return m.group(1)
    return (parts[-1] if parts else "game")[:48]


def provider(url: str) -> str:
    h = urlparse(url).netloc.lower()
    if any(x in h for x in ("pragmatic", "ppassets", "demogamesfree")):
        return "pragmatic"
    if any(x in h for x in ("box-int", "playson", "xplatformwl")):
        return "playson"
    if any(x in h for x in ("3oaks", "softswiss")):
        return "3oaks"
    return "generic"


async def fetch_one(session, url: str, dest: Path):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=11)) as r:
            if r.status != 200:
                return None
            data = await r.read()
            if len(data) < 80:
                return None
            dest.write_bytes(data)
            return dest
    except Exception:
        return None


async def download_all(urls: list, folder: Path) -> list:
    folder.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(DOWNLOAD_WORKERS)
    mapping = {}
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0.0.0"}) as session:
        async def job(u: str):
            async with sem:
                h = hashlib.sha1(u.encode()).hexdigest()[:10]
                ext = Path(urlparse(u).path).suffix or ".bin"
                p = await fetch_one(session, u, folder / ("%s%s" % (h, ext)))
                if p:
                    mapping[u] = p.name
                return p
        results = await asyncio.gather(*[job(u) for u in urls])
    (folder / "mapping.json").write_text(
        __import__("json").dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")
    return [p for p in results if p]


def collect_urls(launch_url: str, budget_sec: float = BUDGET_SEC) -> set:
    crawler = Crawler(budget_ms=int(budget_sec * 1000), port=9333, click=False)
    crawler.run(launch_url)
    return {u for u in getattr(crawler, "urls", set()) if is_spine(u)}


async def process(url: str, executor: ThreadPoolExecutor):
    t0 = time.time()
    prov = provider(url)
    name = game_name(url)
    print("→ [%s] %s" % (prov, name), flush=True)
    loop = asyncio.get_running_loop()
    found = await loop.run_in_executor(executor, collect_urls, url, BUDGET_SEC)
    print("  %d spine-url за %.1fs" % (len(found), time.time() - t0), flush=True)
    if not found:
        return
    out = OUT_DIR / ("%s_%s" % (prov, name))
    saved = await download_all(sorted(found), out)
    print("  сохранено %d → %s" % (len(saved), out), flush=True)


async def main(urls: list):
    OUT_DIR.mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        for url in urls:
            await process(url, pool)


if __name__ == "__main__":
    urls = [u for u in sys.argv[1:] if u.startswith("http")]
    if not urls:
        print("usage: python spine_fast.py <url1> [url2 ...]")
        sys.exit(1)
    asyncio.run(main(urls))
