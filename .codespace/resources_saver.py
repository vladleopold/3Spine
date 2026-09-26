#!/usr/bin/env python3
"""resources_saver.py — полностью автоматическая «сохранялка» на коде
расширения Alex313031/Resources-Saver.

Расширение в браузере требует открытого DevTools и клика, поэтому мы берём его
код (content.js) и запускаем в обычной странице с подменёнными chrome.* API:
    chrome.devtools.inspectedWindow.getResources -> список URL из нашей сессии
    chrome.devtools.network.getHAR              -> то же
    chrome.downloads.download                    -> отдаём байты наружу
Алгоритм отбора, имена и структура папок — оригинальные, расширения.
"""
import asyncio
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"
def _find_chrome() -> str:
    import shutil
    cands = [os.environ.get("SPINE_CHROME", "")]
    for c in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        cands.append(shutil.which(c) or "")
    cands += ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/local/bin/chromium",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return ""


CHROME = _find_chrome()

# что нас интересует: скелеты, атласы, текстуры, манифесты
KEEP_EXT = (".json", ".atlas", ".skel", ".scn", ".png", ".jpg", ".jpeg", ".webp",
            ".ktx", ".ktx2", ".bin", ".data", ".fnt", ".plist", ".pack")
HINT = re.compile(r"spine|skeleton|atlas|res/|assets/|resources|game", re.I)
SKIP_HOST = re.compile(r"google|doubleclick|analytics|facebook|hotjar|clarity|"
                       r"recaptcha|hcaptcha|cookieyes|tiktok|bing|jsdelivr|gstatic",
                       re.I)


def vendor_files() -> tuple:
    js = VENDOR / "rs_content.js"
    html = VENDOR / "rs_content.html"
    if not js.exists():
        import urllib.request
        base = "https://raw.githubusercontent.com/Alex313031/Resources-Saver/main/src/"
        for name, dst in (("content.js", js), ("content.html", html)):
            try:
                data = urllib.request.urlopen(base + name, timeout=60).read()
                dst.write_bytes(data)
            except Exception:                             # noqa: BLE001
                return None, None
    return (js.read_text("utf-8", "replace") if js.exists() else None,
            html.read_text("utf-8", "replace") if html.exists() else "")


SHIM = r"""
window.__rs = {urls: %URLS%, bodies: {}};
window.__rs_out = [];
window.__rs_seen = {};

function __norm(u) { return String(u || '').split('#')[0]; }

window.chrome = window.chrome || {};
chrome.runtime = { getURL: function (p) { return p; } };
chrome.downloads = {
  setShelfEnabled: function () {},
  download: function (opts, cb) {
    var url = opts.url || '';
    var name = opts.filename || 'file';
    if (url.indexOf('blob:') === 0 || url.indexOf('data:') === 0) {
      fetch(url).then(function (r) { return r.arrayBuffer(); })
        .then(function (b) {
          window.__rs_out.push({ name: name, b64: btoa(String.fromCharCode.apply(null,
            new Uint8Array(b))) });
          if (cb) cb(1);
        }).catch(function () {});
    } else {
      window.__rs_out.push({ name: name, url: url });
      if (cb) cb(2);
    }
    return Promise.resolve(1);
  }
};
chrome.tabs = { get: function (id, cb) { cb({ url: window.__rs.page_url || location.href,
  id: id || 1, title: document.title }); }, query: function (q, cb) { cb([]); },
  create: function () {} };
chrome.devtools = {
  inspectedWindow: {
    tabId: 1,
    eval: function (expr, cb) { if (cb) cb({}); },
    getResources: function (cb) { cb(window.__rs.urls.slice()); },
    reload: function () {}
  },
  network: {
    getHAR: function (cb) { cb({ entries: window.__rs.urls.map(function (u) {
      return { request: { url: u } }; }) }); },
    onRequestFinished: { addListener: function () {} }
  },
  panels: { create: function () {} }
};
window.getResources = window.chrome.devtools.inspectedWindow.getResources;
"""


async def save(url: str, out_dir: Path, budget: int = 30000) -> int:
    from playwright.async_api import async_playwright

    content_js, content_html = vendor_files()
    if not content_js:
        print("resources-saver: код расширения недоступен", flush=True)
        return 0

    urls: dict = {}

    async with async_playwright() as p:
        kw = {"headless": True}
        if CHROME and os.path.exists(CHROME):
            kw["executable_path"] = CHROME
        browser = await p.chromium.launch(**kw, args=["--no-sandbox", "--disable-gpu",
                                                      "--disable-dev-shm-usage"])
        ctx = await browser.new_context(ignore_https_errors=True,
                                        user_agent="Mozilla/5.0 (X11; Linux x86_64) "
                                                   "Chrome/131.0.0.0 Safari/537.36")
        page = await ctx.new_page()

        def keep(u: str) -> bool:
            low = u.lower().split("?")[0]
            if not low.startswith(("http://", "https://")):
                return False
            if SKIP_HOST.search(urlsplit(u).netloc):
                return False
            return low.endswith(KEEP_EXT) or bool(HINT.search(low))

        page.on("response", lambda r: urls.__setitem__(r.url, None) if keep(r.url) else None)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception:                                 # noqa: BLE001
            pass
        end = asyncio.get_event_loop().time() + budget / 1000.0
        while asyncio.get_event_loop().time() < end:
            await asyncio.sleep(0.5)
            # лёгкий «клик» по канвасу, чтобы подтолкнуть игру
            for fr in page.frames:
                try:
                    loc = fr.locator("canvas").first
                    if await loc.count():
                        box = await loc.bounding_box()
                        if box:
                            await page.mouse.click(box["x"] + box["width"] / 2,
                                                    box["y"] + box["height"] / 2)
                except Exception:                         # noqa: BLE001
                    pass
        # тела ресурсов забираем из сессии браузера (куки уже применены)
        bodies = {}
        for u in list(urls):
            try:
                resp = await ctx.request.get(u, timeout=30000)
                if resp.status == 200:
                    bodies[u] = await resp.body()
            except Exception:                             # noqa: BLE001
                continue

        collected = []
        await page.expose_function("__rs_collect", lambda name, b64: collected.append((name, b64)))
        await page.set_content(content_html or "<html><body></body></html>")
        await page.add_script_tag(content=SHIM % {"URLS": __import__("json").dumps(sorted(urls))})
        await page.add_script_tag(content=content_js)
        try:
            await page.evaluate("document.getElementById('up-save').click()")
        except Exception as e:                            # noqa: BLE001
            print("resources-saver: клик не сработал: %s" % str(e)[:60], flush=True)
        await asyncio.sleep(3)
        for name, b64 in collected:
            try:
                import base64
                data = base64.b64decode(b64)
            except Exception:                             # noqa: BLE001
                continue
            rel = re.sub(r"[^A-Za-z0-9_./-]+", "_", unquote(name)).lstrip("/")
            dst = out_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        await browser.close()

    # запасной путь: если расширение ничего не отдало — пишем тела напрямую
    if not collected:
        for u, b in bodies.items():
            rel = re.sub(r"[^A-Za-z0-9_./-]+", "_", unquote(urlsplit(u).path.lstrip("/")))
            dst = out_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b)
            collected.append((rel, None))
    print("resources-saver: сохранено %d файлов в %s" % (len(collected), out_dir), flush=True)
    return len(collected)


if __name__ == "__main__":
    target = sys.argv[1]
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "rs_out")
    secs = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    out.mkdir(parents=True, exist_ok=True)
    sys.exit(0 if asyncio.run(save(target, out, secs)) else 2)
