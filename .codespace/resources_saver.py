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
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

HERE = Path(__file__).resolve().parent
BROWSER_PROXY = os.environ.get("SPINE_PROXY_SERVER", "").strip()


def proxy_for_browser() -> dict:
    """Прокси для Chromium: наш edge либо внешний."""
    try:
        sys.path.insert(0, str(HERE))
        from edge_proxy import serve
        import edge_proxy
        return {"server": "http://127.0.0.1:%d" % serve(edge_proxy.free_port(8899))}
    except Exception:                                     # noqa: BLE001
        if BROWSER_PROXY:
            return {"server": BROWSER_PROXY}
        return {}
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
window.__rs = {urls: __URLS__, bodies: {}};
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
        ctx = await browser.new_context(proxy=proxy_for_browser() or None,
                                        ignore_https_errors=True,
                                        extra_http_headers={
                                            "Referer": url,
                                            "Accept": "*/*",
                                            "Accept-Language": "en-US,en;q=0.9",
                                        },
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

        all_urls = []
        frames_seen = set()

        page.on("response", lambda r: (
            all_urls.append(r.url),
            urls.__setitem__(r.url, None) if keep(r.url) else None))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:                            # noqa: BLE001
            print("resources-saver: goto не удался: %s" % str(e)[:60], flush=True)
        for fr in page.frames:
            frames_seen.add(fr.url)
        html_text = ""
        try:
            html_text = await page.content()
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
        # манифест лаунчера: логическое имя -> реальный URL (скачиваем сразу)
        name2url = {}
        for u in list(urls):
            if not u.lower().split("?")[0].endswith((".js", ".json")):
                continue
            try:
                raw = bodies.get(u) or await page.evaluate(
                    "async (x) => await (await fetch(x)).text()", u)
            except Exception:                             # noqa: BLE001
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            if '"files"' not in raw:
                continue
            base = u.rsplit("/", 1)[0] + "/"
            for files, logical in re.findall(
                    r'"files":"([^"]+)"\s*,\s*"path":"([^"]+)"', raw):
                if not files.startswith("res/"):
                    continue
                name = logical.split(":")[-1]
                name2url.setdefault(name, base + files)
                name2url.setdefault(name.rsplit("/", 1)[-1], base + files)
        if name2url:
            print("resources-saver: манифест лаунчера — %d записей" % len(name2url),
                  flush=True)
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
        await page.add_script_tag(content=SHIM.replace("__URLS__",
                                              json.dumps(sorted(urls))))
        await page.add_script_tag(content=content_js)
        try:
            await page.evaluate("document.getElementById('up-save').click()")
        except Exception as e:                            # noqa: BLE001
            print("resources-saver: клик не сработал: %s" % str(e)[:60], flush=True)
        await asyncio.sleep(3)
        for name, b64 in collected:
            try:
                import base64 as _b64
                data = _b64.b64decode(b64)
            except Exception:                             # noqa: BLE001
                continue
            rel = re.sub(r"[^A-Za-z0-9_./-]+", "_", unquote(name)).lstrip("/")
            dst = out_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        await browser.close()

    # докачиваем то, что перечислил манифест, и страницы атласов
    extra = {}
    for name, u in name2url.items():
        if re.search(r"\.(json|atlas|skel|scn)$", name, re.I):
            extra.setdefault(u, name)
    pages = set()
    for name, u in list(extra.items()):
        if not name.lower().endswith(".atlas"):
            continue
        try:
            txt = (bodies.get(u) or b"").decode("utf-8", "replace")
        except Exception:                                 # noqa: BLE001
            txt = ""
        for line in txt.split("\n"):
            head = line.split(":")[0].strip()
            if re.search(r"\.(png|jpg|jpeg|webp)$", head, re.I):
                cand = name2url.get(head)
                if cand:
                    pages.add(cand)
    for u in list(pages):
        extra.setdefault(u, u.rsplit("/", 1)[-1])
    if extra:
        # прямая загрузка из пайплайна работает, а APIRequestContext упирается
        # в 404 — поэтому отдаём карту URL, а тела качает fetch_assets
        import json as _json
        with open(out_dir.parent / "saver-manifest.json", "w", encoding="utf-8") as _f:
            _json.dump({n: u for u, n in extra.items()}, _f, ensure_ascii=False)
        print("resources-saver: карта манифеста — %d файлов (качает конвейер)"
              % len(extra), flush=True)
    if False:
        print("resources-saver: докачиваю по манифесту %d файлов" % len(extra), flush=True)
        sem = asyncio.Semaphore(8)

        import base64 as _b64

        async def grab(u: str, name: str):
            if u in bodies or not keep(u):
                return
            async with sem:
                # качаем из контекста страницы: её заголовки, куки и декодирование
                try:
                    res = await page.evaluate(
                        "async (x) => { const r = await fetch(x, {credentials:'include'});"
                        " if (!r.ok) return null; const b = await r.arrayBuffer();"
                        " let s=''; const u8=new Uint8Array(b);"
                        " for (let i=0;i<u8.length;i+=8192) s += String.fromCharCode.apply(null,"
                        " u8.subarray(i,i+8192)); return btoa(s); }", u)
                    if res:
                        bodies[u] = _b64.b64decode(res)
                except Exception:                         # noqa: BLE001
                    pass
        await asyncio.gather(*[grab(u, n) for u, n in extra.items()], return_exceptions=True)

    # игровые ассеты пишем всегда: наш фильтр строже расширения
    game_files = 0
    for u, b in bodies.items():
        if not keep(u) or not b:
            continue
        rel = name2url_name = None
        for nm, mu in name2url.items():
            if mu == u:
                rel = nm
                break
        rel = rel or re.sub(r"[^A-Za-z0-9_./-]+", "_", unquote(urlsplit(u).path.lstrip("/")))
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b)
        game_files += 1
    print("resources-saver: игровых файлов %d + прочих от расширения %d → %s"
          % (game_files, len(collected), out_dir), flush=True)
    if game_files == 0:
        low = (html_text or "").lower()
        captcha = [m for m in ("captcha", "hcaptcha", "recaptcha", "just a moment",
                               "checking your browser", "cf-browser-verification")
                   if m in low]
        hosts = sorted({u.split("/")[2] for u in all_urls if "://" in u})
        kinds = {}
        for u in urls:
            e = u.rsplit(".", 1)[-1].lower() if "." in u.rsplit("/", 1)[-1] else "?"
            kinds[e] = kinds.get(e, 0) + 1
        print("--- ДИАГНОСТИКА Resources-Saver ---", flush=True)
        print("страница загружена: %s (%d Б)" % (bool(html_text), len(html_text or "")), flush=True)
        print("кадров в браузере: %d | адресов всего: %d | хостов: %d"
              % (len(frames_seen), len(all_urls), len(hosts)), flush=True)
        print("кандидатов по фильтру: %d %s" % (len(urls), kinds), flush=True)
        print("хосты: %s" % ", ".join(hosts[:8]), flush=True)
        print("кадры: %s" % ", ".join(x[:70] for x in list(frames_seen)[:4]), flush=True)
        if captcha:
            print("ВЫВОД: на странице капTCHA/антибот (%s) — браузер без сессии не проходит"
                  % ", ".join(captcha), flush=True)
        elif not all_urls:
            print("ВЫВОД: браузер не сделал ни одного запроса — сайт не отдал страницу", flush=True)
        else:
            print("ВЫВОД: страница открыта, но игровых Spine-ассетов в загрузке нет "
                  "(игра грузится только после ручного входа/клика)", flush=True)
        try:
            import json as _j
            with open(out_dir.parent / "saver-diagnosis.json", "w", encoding="utf-8") as _f:
                _j.dump({"page_loaded": bool(html_text), "page_bytes": len(html_text or ""),
                         "frames": len(frames_seen), "requests": len(all_urls),
                         "hosts": hosts[:12], "candidates": len(urls), "kinds": kinds,
                         "captcha": captcha}, _f, ensure_ascii=False, indent=1)
        except Exception:                                 # noqa: BLE001
            pass
    return game_files + len(collected)


if __name__ == "__main__":
    target = sys.argv[1]
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "rs_out")
    secs = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    out.mkdir(parents=True, exist_ok=True)
    sys.exit(0 if asyncio.run(save(target, out, secs)) else 2)
