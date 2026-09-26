#!/usr/bin/env python3
"""spine_aggressive.py — быстрый сбор Spine-ассетов на Playwright.

Движок не важен: смотрим все запросы всех вкладок (popup included),
разбираем манифесты прямо в ответах, качаем пачкой и проверяем сигнатуры.
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

import aiohttp
from playwright.async_api import async_playwright

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

OUT = Path("spine_out")
WORKERS = 28
BROWSER = (os.environ.get("SPINE_CHROME") or shutil.which("chromium")
           or shutil.which("google-chrome")
           or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
INSECURE = os.environ.get("SPINE_INSECURE") == "1"

try:
    import brotli                                        # noqa: F401
    ACCEPT = "gzip, deflate, br"
except ImportError:
    ACCEPT = "gzip, deflate"

BUDGET = {"pragmatic": 14, "playson": 16, "3oaks": 14, "generic": 16}
CLICK_TEXTS = ("Play", "Start", "Continue", "OK", "Accept", "Spin", "Real Play", "Demo", "Запустить", "Играть")

SPINE_EXT = re.compile(r"\.(skel|json|atlas|png|webp|ktx2?|basis|bin|scn)(\?|$)", re.I)
HINT = re.compile(r"spine|skeleton|symbol|reel|bonus|collect|atlas|frame|clover|attachment|skin|bone", re.I)


def provider(url: str) -> str:
    h = urlparse(url).netloc.lower()
    if any(x in h for x in ("pragmatic", "ppassets", "demogamesfree")):
        return "pragmatic"
    if any(x in h for x in ("box-int", "playson", "xplatformwl")):
        return "playson"
    if any(x in h for x in ("3oaks", "softswiss")):
        return "3oaks"
    return "generic"


def is_spine(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    path = urlparse(url).path.lower()
    if path.endswith((".skel", ".atlas")):
        return True
    if path.endswith(".json"):
        return True                      # хэш-имена: решает проверка сигнатуры
    if path.endswith((".png", ".webp", ".ktx", ".ktx2")) and HINT.search(url):
        return True
    return False


def is_real_spine_file(path: Path) -> bool:
    """Сигнатура: 3.8-бинарник, 4.x-ключи, заголовок атласа."""
    try:
        suf = path.suffix.lower()
        if suf == ".skel":
            b = path.read_bytes()[:80]
            return b"3.8" in b or b"4.0" in b or b"4.1" in b or b[0] in (0x0A, 0x1C, 0x0C)
        if suf == ".atlas":
            t = path.read_text(errors="ignore")[:400]
            return "size:" in t or "format:" in t
        if suf == ".json":
            h = path.read_bytes()[:65536]
            if any(k in h for k in (b'"bones"', b'"slots"', b'"animations"', b'"skins"', b'"skeleton"')):
                return True
            m = re.match(rb"^.[0-9a-f]{6,64}[\x00-\x1f]?(\d\.\d\.\d{2})", h)
            return bool(m) or (bool(re.search(rb"\d\.\d\.\d{2}", h[:256])) and b'"' not in h[:64])
        if suf in (".png", ".webp", ".ktx", ".ktx2"):
            return path.stat().st_size > 200
    except Exception:                                     # noqa: BLE001
        pass
    return False


def safe_name(url: str) -> str:
    h = hashlib.sha1(url.encode()).hexdigest()[:11]
    ext = Path(urlparse(url).path).suffix.lower() or ".bin"
    if len(ext) > 8:
        ext = ".bin"
    return h + ext


async def download_all(urls, folder: Path) -> list:
    folder.mkdir(parents=True, exist_ok=True)
    order = sorted(set(urls), key=lambda u: (0 if u.endswith((".skel", ".atlas"))
                                             else 1 if ".json" in u else 2))
    conn = aiohttp.TCPConnector(limit=0, limit_per_host=8, ttl_dns_cache=600,
                                use_dns_cache=True, keepalive_timeout=30)
    sem = asyncio.Semaphore(WORKERS)
    done = []
    async with aiohttp.ClientSession(
            connector=conn,
            timeout=aiohttp.ClientTimeout(total=None, connect=4, sock_read=15),
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0.0.0",
                     "Accept-Encoding": ACCEPT},
            cookie_jar=aiohttp.DummyCookieJar(), trust_env=False) as session:

        async def grab(u, dest, guard, to):
            """Повторяем только временные сбои: 404/403 — не тратим время."""
            transient = True
            for _ in range(guard):
                try:
                    async with session.get(u, timeout=aiohttp.ClientTimeout(total=to, connect=8,
                                                                            sock_read=to)) as r:
                        if r.status == 200:
                            data = await r.read()
                            if len(data) >= 64:
                                await asyncio.to_thread(dest.write_bytes, data)
                                return True
                            transient = False
                            continue
                        if r.status in (404, 403, 401, 410):
                            return False
                        await asyncio.sleep(0.2)
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    await asyncio.sleep(0.2)
            return False if not transient else False

        async def one(u):
            dest = folder / safe_name(u)
            if dest.exists() and dest.stat().st_size > 64:
                return dest
            async with sem:
                return dest if await grab(u, dest, 2, 15) else None

        for p in await asyncio.gather(*[one(u) for u in order], return_exceptions=True):
            if isinstance(p, Path):
                done.append(p)
        missing = [u for u in order if not (folder / safe_name(u)).exists()]
        if missing:
            print("  добор: %d" % len(missing), flush=True)
            sem2 = asyncio.Semaphore(12)
            for p in await asyncio.gather(*[sem2_wrapped(sem2, grab, u, folder) for u in missing],
                                         return_exceptions=True):
                if isinstance(p, Path) and p not in done:
                    done.append(p)
    return done


async def sem2_wrapped(sem, grab, u, folder):
    dest = folder / safe_name(u)
    async with sem:
        return dest if await grab(u, dest, 1, 12) else None


async def harvest_json_manifest(text: str, base: str, found: set) -> None:
    """Любой JSON-манифест: вытаскиваем ссылки на ассеты."""
    try:
        data = json.loads(text)
    except Exception:                                     # noqa: BLE001
        for m in re.finditer(r'[\w./-]{3,120}\.(?:json|atlas|skel|png|webp|bin|scn)(?:\?[^"\' ]*)?', text):
            found.add(urljoin(base, m.group(0)))
        return

    def walk(obj):
        if isinstance(obj, str):
            if obj.startswith("http"):
                found.add(obj)
            elif SPINE_EXT.search(obj) or HINT.search(obj):
                found.add(urljoin(base, obj))
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)


async def collect(url: str) -> set:
    prov = provider(url)
    budget = BUDGET.get(prov, 16)
    found: set = set()
    early = 30 if prov == "pragmatic" else 80

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=BROWSER, headless=True,
                                          args=["--no-sandbox", "--disable-dev-shm-usage",
                                                "--disable-gpu", "--mute-audio",
                                                "--autoplay-policy=no-user-gesture-required",
                                                "--window-size=1280,900"])
        context = await browser.new_context(viewport={"width": 1280, "height": 900},
                                            user_agent="Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0.0.0",
                                            ignore_https_errors=INSECURE, service_workers="block")

        async def on_request(req):
            if is_spine(req.url):
                found.add(req.url)

        async def on_response(resp):
            if is_spine(resp.url):
                found.add(resp.url)
            u = resp.url
            if u.lower().split("?")[0].endswith((".json", ".js", ".txt")) and \
                    re.search(r"(resources|manifest|config|settings|build|version|index|game)", u, re.I):
                try:
                    body = await resp.text()
                    if len(body) < 40_000_000:
                        await harvest_json_manifest(body, u.rsplit("/", 1)[0] + "/", found)
                except Exception:                         # noqa: BLE001
                    pass

        def attach(page):
            page.on("request", on_request)
            page.on("response", on_response)

        context.on("page", attach)
        page = await context.new_page()
        attach(page)

        try:
            client = await context.new_cdp_session(page)
            await client.send("Network.enable", {"maxTotalBufferSize": 200_000_000,
                                                  "maxResourceBufferSize": 80_000_000})
            await client.send("Network.setCacheDisabled", {"cacheDisabled": True})
            await client.send("Network.setBypassServiceWorker", {"bypass": True})
        except Exception:                                 # noqa: BLE001
            pass

        await page.add_init_script("""
            window.__g = window.__g || [];
            const push = v => { try {
                if (!v) return;
                if (typeof v !== 'string') v = v.url || v.href || String(v);
                if (v.startsWith('http')) window.__g.push(v);
            } catch(e){} };
            const of = window.fetch;
            window.fetch = function(i){ try{ push(typeof i === 'string' ? i : i && i.url); }catch(e){}
                return of.apply(this, arguments); };
            const XO = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(m,u){ try{ push(u); }catch(e){}
                return XO.apply(this, arguments); };
            try { performance.getEntriesByType('resource').forEach(function(e){ push(e.name); });
                new PerformanceObserver(function(l){ l.getEntries().forEach(function(e){ push(e.name); }); })
                    .observe({type:'resource', buffered:true}); } catch(e){}
        """)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=12000)
        except Exception:                                 # noqa: BLE001
            pass

        async def try_click():
            for text in CLICK_TEXTS:
                for pg in context.pages:
                    try:
                        loc = pg.get_by_text(text, exact=False).first
                        if await loc.count() and await loc.is_visible():
                            await loc.click(timeout=700)
                            return
                    except Exception:                     # noqa: BLE001
                        continue
                await asyncio.sleep(0.3)

        asyncio.ensure_future(try_click())

        end = time.time() + budget
        while time.time() < end:
            for pg in context.pages:
                try:
                    for u in await pg.evaluate("() => (window.__g || []).slice(0, 3000)"):
                        if is_spine(u):
                            found.add(u)
                except Exception:                         # noqa: BLE001
                    pass
            if len(found) >= early:
                break
            await asyncio.sleep(0.7)

        await browser.close()
    return found


async def process(url: str):
    t0 = time.time()
    prov = provider(url)
    print("→ [%s] %s…" % (prov, url[:72]), flush=True)
    found = await collect(url)
    print("  %d кандидатов за %.1fs" % (len(found), time.time() - t0), flush=True)
    if not found:
        return
    m = re.search(r"gameName=([\w]+)", url)
    name = m.group(1) if m else (urlparse(url).path.strip("/").replace("/", "_")[:40] or "game")
    raw = OUT / ("%s_%s" % (prov, name))
    saved = await download_all(sorted(found), raw)
    print("  скачано %d → %s" % (len(saved), raw), flush=True)
    ver = OUT / "verified" / ("%s_%s" % (prov, name))
    ver.mkdir(parents=True, exist_ok=True)
    ok = 0
    for f in saved:
        if is_real_spine_file(f):
            dst = ver / f.name
            if not dst.exists():
                dst.write_bytes(f.read_bytes())
            ok += 1
    print("  ✓ Spine: %d → %s" % (ok, ver), flush=True)


async def main(urls):
    OUT.mkdir(exist_ok=True)
    for u in urls:
        await process(u)


if __name__ == "__main__":
    urls = [u for u in sys.argv[1:] if u.startswith("http")]
    if not urls:
        print("usage: python spine_aggressive.py <url1> [url2 ...]")
        sys.exit(1)
    asyncio.run(main(urls))
