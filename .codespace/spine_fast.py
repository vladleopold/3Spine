#!/usr/bin/env python3
"""spine_fast.py — скоростной сборщик Spine-ассетов.

Работает с .codespace/cdp.py (Crawler). Требования: только браузер + сеть,
никаких знаний о конкретной площадке.
"""
from __future__ import annotations
import asyncio
import hashlib
import os
import shutil
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

try:                                                     # brotli ускоряет, но не обязателен
    import brotli                                        # noqa: F401
    ACCEPT_ENCODING = "gzip, deflate, br"
except ImportError:
    ACCEPT_ENCODING = "gzip, deflate"

OUT_DIR = Path("spine_out")
BUDGET_SEC = 16            # жёсткий лимит на одну игру
DOWNLOAD_WORKERS = 14
SPINE_RE = re.compile(r'\.(skel|json|atlas|png|webp|ktx2?|basis|data|bin|bundle)(\?|$)', re.I)
HINT_RE = re.compile(
    r'spine|skeleton|atlas|attachment|bone|slot|skin|symbol|reel|bonus|collect|frame', re.I)


def is_spine(url: str) -> bool:
    """Строгий отбор кандидатов: skel/atlas — сразу, json и текстуры — по ключам."""
    if not url or not url.startswith("http"):
        return False
    u = url.lower()
    path = urlparse(url).path.lower()
    if path.endswith((".skel", ".atlas")):
        return True
    if path.endswith(".json"):
        # имена часто хэшированные — берём всё, авторитетна проверка сигнатуры
        keys = ("spine", "skeleton", "symbol", "reel", "bonus", "collect",
                "animation", "bone", "slot", "skin", "clover", "frame",
                "attachment", "ik", "transform", "res/", "assets/", "res/import",
                "spine", "skel", "atlas")
        return True if any(k in u for k in keys) else True
    if path.endswith((".png", ".webp", ".ktx", ".ktx2", ".basis")):
        keys = ("spine", "symbol", "atlas", "reel", "bonus", "skin", "collect", "clover")
        return any(k in u for k in keys)
    return False


def is_real_spine_file(path: Path) -> bool:
    """Проверка сигнатуры: bones/slots/animations или заголовок атласа."""
    try:
        if path.suffix.lower() == ".skel":
            data = path.read_bytes()[:64]
            return b"3.8" in data or b"4.0" in data or b"4.1" in data or data[0] in (0x0a, 0x1c, 0x0c)
        if path.suffix.lower() == ".atlas":
            head = path.read_text(errors="ignore")[:500]
            return "size:" in head or "format:" in head or "filter:" in head
        if path.suffix.lower() == ".json":
            head = path.read_bytes()[:65536]
            # 4.x — обычный текстовый json
            if any(k in head for k in (b'"bones"', b'"slots"', b'"animations"',
                                       b'"skins"', b'"skeleton"')):
                return True
            # 3.8 — бинарный: длина хэша, хэш, версия вида 3.8.99
            m = re.match(rb"^.[0-9a-f]{6,64}[\x00-\x1f]?(\d\.\d\.\d{2})", head)
            if m:
                return True
            return bool(re.search(rb"\d\.\d\.\d{2}", head[:256])) and b'"' not in head[:64]
        if path.suffix.lower() in (".png", ".webp", ".ktx", ".ktx2", ".basis"):
            return path.stat().st_size > 200
    except Exception:                                     # noqa: BLE001
        pass
    return False


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


def priority(url: str) -> int:
    p = urlparse(url).path.lower()
    if p.endswith((".skel", ".atlas")):
        return 0
    if p.endswith(".json"):
        return 1
    return 2


def _guard_ok(url: str, data: bytes) -> bool:
    """Отсекаем soft-404/HTML, которые CDN отдаёт с кодом 200."""
    try:
        sys.path.insert(0, str(HERE))
        from fetch_guard import check
    except Exception:                                     # noqa: BLE001
        return True
    ok, _why = check(urlparse(url).path, data)
    return ok


def local_name(url: str) -> str:
    h = hashlib.sha1(url.encode()).hexdigest()[:11]
    ext = Path(urlparse(url).path).suffix.lower()
    if ext not in (".skel", ".json", ".atlas", ".png", ".webp", ".ktx", ".ktx2", ".basis", ".bin"):
        ext = ".bin"
    return "%s%s" % (h, ext)


async def download_all(urls: list, folder: Path) -> list:
    """Быстрое скачивание: приоритет .skel/.atlas -> .json -> текстуры, добор отброшенных."""
    import json as _json
    folder.mkdir(parents=True, exist_ok=True)
    uniq = sorted(set(urls), key=priority)
    mapping = {}

    if shutil.which("aria2c") and os.environ.get("SPINE_ARIA2", "0") == "1":
        uf = folder / "_urls.txt"
        uf.write_text("\n".join(uniq), encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            "aria2c", "-i", str(uf), "-j", "32", "-x", "6", "-s", "6", "-c",
            "--max-tries=2", "--retry-wait=1", "--connect-timeout=3", "--timeout=10",
            "--dir", str(folder), "--auto-file-renaming=false", "--allow-overwrite=true",
            "--console-log-level=error", "--summary-interval=0")
        await proc.wait()

    else:
        try:
            import uvloop
            uvloop.install()
        except Exception:                                 # noqa: BLE001
            pass
        WORKERS, CONNECT_TIMEOUT, READ_TIMEOUT, DNS_CACHE = 24, 4.0, 15, 600
        conn = aiohttp.TCPConnector(limit=0, limit_per_host=8, ttl_dns_cache=DNS_CACHE,
                                    use_dns_cache=True, enable_cleanup_closed=True,
                                    force_close=False, keepalive_timeout=30)
        timeout = aiohttp.ClientTimeout(total=None, connect=CONNECT_TIMEOUT,
                                        sock_read=READ_TIMEOUT)
        sem = asyncio.Semaphore(WORKERS)

        async with aiohttp.ClientSession(
                connector=conn, timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0.0.0",
                         "Accept-Encoding": ACCEPT_ENCODING, "Connection": "keep-alive"},
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False) as session:

            async def grab(url: str, dest: Path, guard: int, to: float) -> bool:
                for _ in range(guard):
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(
                                total=to, connect=8, sock_read=to)) as r:
                            if r.status != 200:
                                continue
                            ctype = (r.headers.get("Content-Type") or "").lower()
                            if "html" in ctype and not url.lower().endswith((".html", ".htm")):
                                return False
                            data = await r.read()
                            if len(data) < 64 or not _guard_ok(url, data):
                                continue
                            await asyncio.to_thread(dest.write_bytes, data)
                            return True
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                        await asyncio.sleep(0.2)
                return False

            async def one(url: str):
                dest = folder / local_name(url)
                if dest.exists() and dest.stat().st_size > 64:
                    return dest
                async with sem:
                    if await grab(url, dest, 2, READ_TIMEOUT):
                        return dest
                return None

            tasks = [asyncio.create_task(one(u)) for u in uniq]
            done = []
            for fut in asyncio.as_completed(tasks):
                p = await fut
                if p:
                    done.append(p)

            missing = [u for u in uniq
                       if not (folder / local_name(u)).exists()
                       or (folder / local_name(u)).stat().st_size <= 64]
            if missing:
                print("  добор: %d файлов" % len(missing), flush=True)
                sem2 = asyncio.Semaphore(6)
                for batch in [missing[i:i + 60] for i in range(0, len(missing), 60)]:
                    async def slow(u: str):
                        dest = folder / local_name(u)
                        async with sem2:
                            return dest if await grab(u, dest, 3, 30) else None
                    for fut in asyncio.as_completed([asyncio.create_task(slow(u)) for u in batch]):
                        await fut

    for u in uniq:
        f = folder / local_name(u)
        if f.exists() and f.stat().st_size > 64:
            mapping[u] = f.name
    (folder / "mapping.json").write_text(_json.dumps(mapping, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    return [folder / local_name(u) for u in uniq
            if (folder / local_name(u)).exists()]


def deep_pass(url: str, budget_ms: int = 20000) -> str:
    """Глубокий уровень: манифест + эскалация бюджета (fetch_assets.py)."""
    import subprocess
    out = str(OUT_DIR / "deep.zip")
    subprocess.run([sys.executable, str(HERE / "fetch_assets.py"), url,
                    "-o", out, "--budget-ms", str(budget_ms)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return out


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
    print("  %d кандидатов за %.1fs" % (len(found), time.time() - t0), flush=True)
    if not found:
        return
    out = OUT_DIR / ("%s_%s" % (prov, name))
    saved = await download_all(sorted(found), out)
    print("  скачано %d → %s" % (len(saved), out), flush=True)
    verified = OUT_DIR / "verified" / ("%s_%s" % (prov, name))
    verified.mkdir(parents=True, exist_ok=True)
    ok = 0
    for p in saved:
        if is_real_spine_file(p):
            dst = verified / p.name
            if not dst.exists():
                dst.write_bytes(p.read_bytes())
            ok += 1
    print("  ✓ настоящих Spine: %d → %s" % (ok, verified), flush=True)
    if os.environ.get("SPINE_DEEP", "1") == "1":
        import subprocess
        z = deep_pass(url)
        if os.path.exists(z):
            import zipfile
            names = zipfile.ZipFile(z).namelist()
            sj = [n for n in names if n.endswith(".json")]
            sa = [n for n in names if n.endswith(".atlas")]
            sp = [n for n in names if n.endswith(".png")]
            reels = [n for n in names if "reels" in n]
            print("  глубокий уровень: %d файлов (%d json, %d atlas, %d png), reels: %d"
                  % (len(names), len(sj), len(sa), len(sp), len(reels)), flush=True)


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
