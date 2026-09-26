#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Выкачивание ассетов игры по ссылке запуска.
#
# Схема: страница запуска → <base href> + launcher.<hash>.js → манифест
# «хеш-файл ↔ логический путь» → скачивание нужных файлов в дерево по
# логическим путям → zip для конвейера.
#
# Скачиваются только ассеты Spine: .json (бинарные скелеты Spine лежат под
# расширением .json), .atlas, .atlas.txt и картинки. Звук/шрифты/конфиги
# пропускаются — они не нужны для конвертации.
#
# Использование:
#   python3 fetch_assets.py "<launch-url>" -o input.zip [--max-mb 300] [--kinds json,atlas,png]
import argparse
import io
import subprocess
import tempfile
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from urllib.parse import urljoin, urlsplit
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".ktx", ".pvr", ".avif")
SPINE_EXT = (".json", ".atlas", ".atlas.txt")
NORMAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
DEFAULT_KINDS = ("json", "atlas", "png")
REJECTED = []
MANIFESTS = []
REMOTE = {}
NAME2URL = {}
URL2NAME = {}


def _guard_state(url: str, data: bytes) -> str:
    """ok | manifest | reject — режем soft-404/HTML, манифесты кладём отдельно."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fetch_guard import classify_response
    except Exception:                                     # noqa: BLE001
        return "ok"
    path = url.split("://", 1)[-1].split("/", 1)[-1]
    return classify_response(path, data)


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_launch(url: str):
    """Возвращает (base_url, launcher_url) по странице запуска."""
    html = fetch(url).decode("utf-8", "replace")
    m = re.search(r'<base[^>]+href="([^"]+)"', html)
    if m:
        base = m.group(1)
    else:
        from urllib.parse import urlparse, urljoin
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}/"
    if not base.endswith("/"):
        base += "/"
    srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
    launcher = None
    for s in srcs:
        if "launcher" in s or s.endswith(".js"):
            launcher = s if s.startswith("http") else base + s.lstrip("./")
            break
    return base, launcher, html


def parse_manifest(js: str):
    """Пары (хеш-путь, логический путь) из бандла лаунчера."""
    pairs = re.findall(r'"files":"([^"]+)"\s*,\s*"path":"([^"]+)"', js)
    out = []
    for files, logical in pairs:
        if not files.startswith("res/"):
            continue
        name = logical.split(":")[-1]           # game:res/spine/x/x.json → res/spine/x/x.json
        out.append((files, name))
    return out


def manifest_index(urls) -> dict:
    """Логическое имя -> реальный URL, из бандла лаунчера (Cocos/Unity Web и т.п.)."""
    out = {}
    for u in urls:
        low = u.lower()
        if not low.endswith((".js", ".json")) or not re.search(r"(launcher|main|settings|bundle|config|index)", low):
            continue
        try:
            body = fetch(u, timeout=40).decode("utf-8", "replace")
        except Exception:                                 # noqa: BLE001
            continue
        if '"files"' not in body:
            continue
        base = u.rsplit("/", 1)[0] + "/"
        for files, name in parse_manifest(body):
            full = base + files
            out.setdefault(name, full)
            out.setdefault(name.rsplit("/", 1)[-1], full)
            URL2NAME.setdefault(full, name)          # чтобы сохранять под логическим именем
    return out


def wanted(name: str, kinds) -> bool:
    low = name.lower()
    if low.endswith(".atlas") or low.endswith(".atlas.txt"):
        return "atlas" in kinds
    if low.endswith(".json"):
        return "json" in kinds
    if low.endswith(IMAGE_EXT):
        return "png" in kinds
    return False


# ── универсальный захват через headless-браузер ───────────────────────────
# Любая браузерная игра (Cocos, Unity, Phaser, свой движок) грузит ассеты
# запросами. Chrome в режиме headnew пишет net-log со всеми URL — этого
# достаточно, чтобы узнать, где лежат .skel/.atlas/.json/.png, без знания
# устройства конкретной площадки.
CHROME_CANDIDATES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "/usr/bin/google-chrome", "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
# маркеры движков: по ним решаем, есть ли вообще что извлекать
ENGINE_MARKERS = {
    "unity": (".framework.js", ".loader.js", ".wasm", ".data", "build/unity"),
    "construct": ("constructjs", "c3runtime", "rkwebgl"),
    "phaser": ("phaser.", "phaser.min.js"),
    "cocos": ("cocos2d", "cocos-js", "res/import", "spine"),
    "egret": ("egret", "default.res.json"),
    "pixi": ("pixi.js", "pixi.min.js"),
    "three": ("three.min.js", "three.module.js"),
    "gDevelop": ("gdevelop", "gdjs_"),
}
SPINE_HINTS = ("spine", "skeleton", "skel", "atlas", "anim", "character", "hero")
# мусор площадок: логотипы/шрифты/аналитика, а не игровые ассеты
JUNK_HINTS = (
    "favicon", "logo", "google", "googletagmanager", "doubleclick", "facebook",
    "recaptcha", "yandex", "cookie", "fontawesome", "gstatic", "jsdelivr",
    "bootstrap", "jquery", "font/", ".woff", ".svg", "sentry", "hotjar",
    "font-", "webfont", "1x1.", "pixel", "spacer", "blank.gif",
)


def find_chrome() -> str:
    import shutil
    for c in CHROME_CANDIDATES:
        p = shutil.which(c) if not c.startswith("/") else (c if os.path.exists(c) else None)
        if p:
            return p
    return ""


def netlog_urls(url: str, netlog: str, budget_ms: int) -> list:
    """Снимаем все сетевые запросы страницы через net-log Chrome.

    Бюджет — реальное время: запускаем Chrome, ждём budget_ms и снимаем его
    сами. Chrome с --virtual-time-budget на тяжёлых SPA (капча, вебсокеты)
    не завершается сам и висит до бесконечности, из-за чего обрывается всё.
    """
    chrome = find_chrome()
    if not chrome:
        return []
    prof = os.path.join(os.path.dirname(netlog), "chrome-%d" % (budget_ms % 100000))
    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
           "--disable-extensions", "--mute-audio", "--hide-scrollbars",
           "--autoplay-policy=no-user-gesture-required",
           "--user-agent=" + NORMAL_UA, "--lang=en-US", "--window-size=1280,900",
           "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
           "--disable-features=IsolateOrigins,site-per-process",
           "--user-data-dir=" + prof, "--log-net-log=" + netlog, url]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:                                     # noqa: BLE001
        return []
    deadline = time.time() + budget_ms / 1000.0
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.4)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except Exception:                                 # noqa: BLE001
            proc.kill()
            try:
                proc.wait(timeout=4)
            except Exception:                             # noqa: BLE001
                pass
    time.sleep(0.6)                                       # даём дописать net-log
    out = set()
    for path in (netlog, netlog + ".1"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError:
            continue
        for m in re.finditer(r'"url":\s*"([^"]+)"', raw):
            u = m.group(1).replace("\\\\/", "/")
            if u.startswith(("http://", "https://")):
                out.add(u.split("#")[0])
        if out:
            break
    return sorted({u.split("?")[0] for u in out})


SHELL_RE = re.compile(
    r"https?://[^\"'\s\\]{6,240}?(?:openGame|html5Game|gameService|game\.do|"
    r"play\.do|launch\.do|/game/|/play/|/launch/|/portal/)[^\"'\s\\]{0,200}", re.I)


def shell_candidates(url: str, urls) -> list:
    """Ищем адрес шелла игры статически: в HTML и в мелких JS.

    Нужно, когда игра не стартует в headless (антибот, hCaptcha) — шелл
    всё равно лежит в коде страницы, а из него уже видно структуру ассетов.
    """
    out, seen = [], set()

    def add(u: str) -> None:
        u = u.rstrip('",\');')
        if not u.lower().startswith(("http://", "https://")):
            return
        base = u.split("?")[0]
        if base in seen:
            return
        seen.add(base)
        out.append(u)

    try:
        html = fetch(url, timeout=30).decode("utf-8", "replace")
    except Exception:                                     # noqa: BLE001
        html = ""
    for m in SHELL_RE.finditer(html):
        add(m.group(0))
    for u in list(urls):
        if not u.lower().endswith((".js", ".html")):
            continue
        try:
            raw = fetch(u, timeout=20)                    # один запрос на файл
            if len(raw) > 3 * 1024 * 1024 or not SHELL_RE.search(
                    raw[:400000].decode("utf-8", "replace")):
                continue
            body = raw.decode("utf-8", "replace")
        except Exception:                                 # noqa: BLE001
            continue
        for m in SHELL_RE.finditer(body):
            add(m.group(0))
        if len(out) >= 6:
            break
    return out[:6]


IFRAME_RE = re.compile(
    r'<iframe[^>]+src=["\']([^"\']+)["\']|'
    r'<frame[^>]+src=["\']([^"\']+)["\']|'
    r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\'\s>]+)|'
    r'(?:src|href|url|gameUrl|game_url|srcGame|game)\s*[:=]\s*["\']([^"\']+\.(?:js|json|html|do|php)(?:\?[^"\']*)?)["\']',
    re.I)


def html_links(url: str, html: str) -> list:
    """Ссылки-кандидаты: iframe, meta-refresh, url= в редиректах, src= в скриптах."""
    out = []
    for m in IFRAME_RE.finditer(html):
        for g in m.groups():
            if g:
                out.append(g.replace("&amp;", "&"))
    return out[:40]


def detect_engine(urls) -> str:
    blob = " ".join(urls).lower()
    for name, marks in ENGINE_MARKERS.items():
        if any(m in blob for m in marks):
            return name
    return "unknown"


def looks_junk(u: str) -> bool:
    low = u.lower()
    if any(j in low for j in JUNK_HINTS):
        return True
    base = low.rsplit("/", 1)[-1]
    return "." not in base


def pick_assets(urls, kinds) -> dict:
    """Отбор ассетов, одинаково пригодный для любого движка.

    spine-подобное определяем по расширениям и по подсказкам в пути;
    json берём только если рядом есть atlas/скелет или в пути есть spine-маркер,
    иначе это конфиги движка (spritesheet, settings) — они в конвейере бесполезны.
    """
    atlas = {u for u in urls if u.lower().endswith((".atlas", ".atlas.txt", ".fnt"))}
    skels = {u for u in urls if u.lower().endswith((".skel", ".scn"))}
    jsons, pages, others = [], [], []
    for u in urls:
        low = u.lower()
        if looks_junk(u):
            continue
        if low.endswith((".png", ".jpg", ".jpeg", ".webp", ".ktx")):
            pages.append(u)
        elif low.endswith((".json", ".data", ".unityweb", ".bundle", ".bin", ".pck", ".zip")):
            hint = any(h in low for h in SPINE_HINTS)
            sib = any(u.rsplit("/", 1)[0] + "/" + a for a in list(atlas) + list(skels)
                      if "/" in a)
            if hint or sib or u in skels:
                jsons.append(u)
            else:
                others.append(u)
        elif low.endswith((".mp3", ".ogg", ".m4a", ".txt", ".fnt", ".xml")):
            others.append(u)
    out = {"atlas": sorted(atlas), "skel": sorted(skels)}
    out["json"] = sorted(jsons) if ("json" in kinds or not kinds) else []
    if "png" in kinds:
        out["png"] = sorted(pages)
    out["_other"] = sorted(others)
    return out


def discover(url: str, tmp: str, budget_ms: int = 18000, depth: int = 2,
             args_passes: int = 3, args_cdp: int = 1) -> dict:
    """Универсальное обнаружение: браузер (все фреймы) + рекурсивный обход HTML."""
    seen_pages, queue, urls = set(), [(url, 0)], set(urls0 := [url])
    netlog = os.path.join(tmp, "netlog.json")
    got = netlog_urls(url, netlog, budget_ms)
    urls |= set(got)
    # CDP-хук дополняет netlog: blob/object-URL и всё, что грузится позже
    if args_cdp:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from cdp import Crawler
            extra = Crawler(budget_ms=budget_ms, click=False).run(url, lognet="")
            if extra.get("urls"):
                add = len(set(extra["urls"]) - urls)
                log("хук в браузере: +%d адресов%s" % (add, (" (заметки: %s)" % extra["notes"][0][:60]) if extra.get("notes") else ""))
            urls |= set(extra.get("urls", []))
        except Exception as e:                             # noqa: BLE001
            log("хук CDP не сработал: %s" % e)
    pages_from_browser = {u for u in got if u not in urls0}

    # шеллы игры, найденные статически (работает без запуска игры)
    shells = shell_candidates(url, urls)
    if shells:
        log("шелл игры найден статически: %s" % shells[0][:100])

    # второй проход: игра почти всегда живёт во внутренней странице/iframe,
    # и её ассеты грузятся уже без обёртки сайта
    if args_passes > 1:
        page_ext = (".html", ".htm", ".xhtml", ".php", ".do", ".asp", ".aspx", "/")
        kids = list(shells) + [u for u in got
                if u not in urls0 and not looks_junk(u)
                and u.lower().split("?")[0].endswith(page_ext)
                and re.search(r"(openGame|html5Game|play|game|launch|portal|index|shell|\.do|\.html|\.php)", u, re.I)
                and not re.search(r"(balance|stats|settings|service|unread|reload|save|track|report|"
                                  r"log|notify|ping|heartbeat|api/)", u, re.I)]
        kids = sorted(set(kids), key=len, reverse=True)[:args_passes - 1]
        for i, kid in enumerate(kids):
            log("проход браузера %d/%d: %s" % (i + 2, args_passes, kid[:110]))
            more = netlog_urls(kid, os.path.join(tmp, "netlog%d.json" % (i + 2)), budget_ms)
            new = [u for u in more if u not in urls]
            if new:
                log("  +%d адресов" % len(new))
            urls |= set(more)

    while queue and len(seen_pages) < 8:
        page, lvl = queue.pop(0)
        if page in seen_pages or not page.lower().split("?")[0].endswith((".html", ".htm", ".php", ".do", "/")):
            seen_pages.add(page)
            continue
        seen_pages.add(page)
        try:
            html = fetch(page, timeout=30).decode("utf-8", "replace")
        except Exception:                                # noqa: BLE001
            continue
        for link in html_links(page, html):
            if not link.lower().startswith(("http://", "https://", "/")):
                base = re.match(r"(https?://[^/]+/)?", link)
                link = base.group(1) + link if base and base.group(1) else link
            nxt = urljoin(page, link)
            nxt = nxt.split("?")[0].split("#")[0] if not nxt.lower().endswith((".do", ".php")) else nxt
            if nxt not in urls and not looks_junk(nxt):
                urls.add(nxt)
                if lvl + 1 <= depth and len(queue) < 6:
                    queue.append((nxt, lvl + 1))
    return {"urls": urls | set(shells), "pages": pages_from_browser, "shells": shells,
            "engine": detect_engine(urls)}


def atlas_page_refs(atlas_text: str) -> list:
    names = []
    for line in atlas_text.split("\n"):
        for part in re.split(r"[,\s]+", line.strip()):
            if re.search(r"\.(png|jpg|jpeg|webp)$", part, re.I):
                names.append(part.strip())
    return names


DEFAULT_KINDS = ("json", "atlas", "png")


REF_RE = re.compile(r"[A-Za-z0-9_./\\-]{1,140}\.(?:atlas|skel|scn|json|png|webp|ktx)(?:\.txt)?", re.I)
PACKED_EXT = (".data", ".unityweb", ".bundle", ".bin", ".pck", ".zip", ".pak", ".db", ".assets")


def text_refs(blob: bytes) -> set:
    """Ссылки на ассеты внутри текстов: json/js/html/manifest."""
    try:
        t = blob.decode("utf-8", "replace")
    except Exception:                                     # noqa: BLE001
        return set()
    return {m.group(0).lstrip("./") for m in REF_RE.finditer(t)}


def binary_refs(blob: bytes) -> set:
    """Имена ассетов внутри бинарных бандлов: ascii и utf-16le строки."""
    out = set()
    for m in re.finditer(rb"[\x20-\x7e]{4,160}", blob):
        t = m.group(0).decode("ascii", "ignore")
        if re.search(r"\.(atlas|skel|scn|json)\b", t, re.I):
            out.update(r.lstrip("./") for r in REF_RE.findall(t))
    for m in re.finditer(rb"(?:[\x20-\x7e]\x00){4,160}", blob):
        t = m.group(0).decode("utf-16le", "ignore")
        if re.search(r"\.(atlas|skel|scn|json)\b", t, re.I):
            out.update(r.lstrip("./") for r in REF_RE.findall(t))
    return out


def bases_of(urls) -> list:
    """Каталоги, относительно которых разумно искать файлы."""
    seen, out = set(), []
    for u in urls:
        d = u.rsplit("/", 1)[0]
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


SPINE_WORD = re.compile(r"spine|skeleton|skel|atlas|anim|bone|slot|skin|character|hero", re.I)
REF_CAP = 400
CAND_CAP = 700


def resolve_refs(refs, bases) -> list:
    """Строим осмысленный список проб: только Spine-подобные имена, с приоритетом."""
    scored = []
    for r in refs:
        low = r.lower()
        score = 0
        if SPINE_WORD.search(low):
            score += 3
        if low.endswith((".atlas", ".skel", ".scn")):
            score += 2
        if low.endswith(".json"):
            score += 1
        if score:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    scored = scored[:REF_CAP]
    spine_bases = [b for b in bases if SPINE_WORD.search(b)]
    ordered_bases = spine_bases + [b for b in bases if b not in spine_bases]
    out, seen = [], set()
    for _score, r in scored:
        stem = re.sub(r"\.(atlas|skel|scn|json|png|webp|ktx)(\.txt)?$", "", r, flags=re.I)
        tail = r.rsplit("/", 1)[-1]
        cands = [r]
        for b in ordered_bases:
            if "/" in r:
                cands.append(b + "/" + tail)
            else:
                cands.append(b + "/" + r)
            for ext in (".json", ".atlas", ".skel"):
                cands.append(b + "/" + stem + ext)
        for c in cands:
            if c.lower().startswith(("http://", "https://")) and c not in seen:
                seen.add(c)
                out.append(c)
                if len(out) >= CAND_CAP:
                    return out
    return out


HEAD_CACHE = {}


def url_ok_many(us: list, workers: int = 16) -> list:
    from concurrent.futures import ThreadPoolExecutor
    us = [u for u in us if not looks_junk(u)]
    if not us:
        return []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return [u for u, ok in zip(us, ex.map(url_ok, us)) if ok]


def url_ok(u: str) -> bool:
    """Мягкая проверка существования: HEAD, при отказе — GET нулевого диапазона."""
    if u in HEAD_CACHE:
        return HEAD_CACHE[u]
    ok = False
    try:
        req = urllib.request.Request(u, method="GET", headers={
            "User-Agent": NORMAL_UA, "Range": "bytes=0-1"})
        with urllib.request.urlopen(req, timeout=12) as r:
            ok = r.status < 400
    except Exception:                                     # noqa: BLE001
        ok = False
    HEAD_CACHE[u] = ok
    return ok


def download_set(urls, root: str, workers: int, timeout: int, log_prefix: str) -> int:
    from concurrent.futures import ThreadPoolExecutor
    os.makedirs(root, exist_ok=True)
    done = [0]

    def one(u: str):
        rel = URL2NAME.get(u) or u.split("://", 1)[-1].split("/", 1)[-1] or "index"
        rel = re.sub(r"[\\:*?\"<>|]", "_", rel)
        dst = os.path.join(root, rel)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            done[0] += 1
            return 0
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            data = fetch(u, timeout=timeout)
        except Exception:                                # noqa: BLE001
            return 0
        state = _guard_state(u, data)
        if state == "reject":
            REJECTED.append((u, "мусор"))
            return 0
        if state == "manifest":
            mdir = os.path.join(root, "_manifests", os.path.dirname(rel))
            os.makedirs(mdir, exist_ok=True)
            with open(os.path.join(mdir, os.path.basename(rel)), "wb") as f:
                f.write(data)
            MANIFESTS.append(u)
            return 0
        with open(dst, "wb") as f:
            f.write(data)
        REMOTE[dst] = u
        done[0] += 1
        if done[0] % 25 == 0:
            log("%s скачано %d" % (log_prefix, done[0]))
        return 1

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, urls))
    return done[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Универсальная выкачка игровых ассетов по ссылке")
    ap.add_argument("url", help="ссылка на игру (любую страницу, где она играется)")
    ap.add_argument("-o", "--out", default="input.zip")
    ap.add_argument("--max-mb", type=int, default=300)
    ap.add_argument("--kinds", default=",".join(DEFAULT_KINDS))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--budget-ms", type=int, default=26000, help="сколько ждём браузер на страницу")
    ap.add_argument("--depth", type=int, default=2, help="глубина обхода HTML")
    ap.add_argument("--scan", type=int, default=1, help="1 — автономный скан ссылок и бандлов")
    ap.add_argument("--scan-texts", type=int, default=60, help="сколько текстовых файлов читать")
    ap.add_argument("--probe-limit", type=int, default=400, help="потолок сетевых проб")
    ap.add_argument("--passes", type=int, default=3, help="сколько страниц обходить браузером")
    ap.add_argument("--cdp", type=int, default=0, help="1 — лёгкий CDP-хук поверх netlog")
    ap.add_argument("--escalate", type=int, default=1, help="1 — углублять обход при неполноте")
    ap.add_argument("--min-json", type=int, default=12, help="меньше этого — считаем неполным")
    ap.add_argument("--inline", type=int, default=1, help="1 — извлекать ассеты, вшитые в манифесты")
    ap.add_argument("--manifests", type=int, default=60, help="сколько манифестов качать")
    ap.add_argument("--grid", type=int, default=1, help="1 — зондировать сетку манифестов")
    ap.add_argument("--grid-dirs", type=int, default=20, help="сколько каталогов проверять")
    ap.add_argument("--grid-count", type=int, default=40, help="сколько файлов в сетке на каталог")
    ap.add_argument("--grid-probe", type=int, default=900, help="потолок сетевых проб")
    ap.add_argument("--probe-workers", type=int, default=20, help="потоков для проб")
    args = ap.parse_args()

    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    started = time.time()
    tmp = tempfile.mkdtemp(prefix="gfetch-")
    limit = args.max_mb * 1048576
    log("ссылка: %s" % args.url)
    log("браузер: %s" % (find_chrome() or "не найден — только статический обход"))

    info = discover(args.url, tmp, args.budget_ms, args.depth, args.passes, args.cdp)
    urls = info["urls"]
    log("найдено адресов: %d (движок: %s)" % (len(urls), info["engine"]))
    picked = pick_assets(urls, kinds)
    log("кандидаты: json=%d atlas=%d skel=%d картинки=%d (прочее отброшено: %d)" % (
        len(picked["json"]), len(picked["atlas"]), len(picked["skel"]),
        len(picked.get("png", [])), len(picked["_other"])))

    # быстрый проход не дал полноты -> углубляемся автоматически
    if args.escalate and not (picked["atlas"] and len(picked["json"]) >= args.min_json):
        deep = min(args.budget_ms * 3, 90000)
        log("быстрый проход неполный (%d json, %d atlas) -> углубляюсь до %d мс"
            % (len(picked["json"]), len(picked["atlas"]), deep))
        info2 = discover(args.url, tmp, deep, args.depth, args.passes, args.cdp)
        urls |= info2["urls"]
        picked = pick_assets(urls, kinds)
        log("после углубления: json=%d atlas=%d skel=%d" % (
            len(picked["json"]), len(picked["atlas"]), len(picked["skel"])))

    root = os.path.join(tmp, "assets")
    # индекс манифеста лаунчера: логическое имя -> реальный URL
    NAME2URL.update(manifest_index(urls))
    if NAME2URL:
        log("манифест лаунчера: %d записей" % len(NAME2URL))
        extra = [u for name, u in NAME2URL.items()
                 if re.search(r"\.(json|atlas|skel)$", name, re.I)
                 and re.search(r"(spine|skel|atlas|anim|bone|skin)", name, re.I)]
        if extra:
            log("из манифеста добавлено кандидатов: %d" % len(extra))
            picked["json"] = sorted(set(picked["json"]) | set(extra))

    # манифесты движков: могут не грузиться при старте, но в них бывают ассеты
    manifests = [u for u in urls
                 if u.lower().split("?")[0].endswith((".json", ".manifest", ".txt"))
                 and re.search(r"(resource|manifest|config|settings|version|build|index|"
                               r"data|bundle|main|game)", u, re.I)][:args.manifests]
    log("манифестов среди найденных: %d" % len(manifests))

    # зондируем сетку манифестов: <dir>/main_resources000.json, *_resourcesNNN.json
    grid = []
    if args.grid:
        all_dirs = bases_of(urls)
        game_dirs = [d for d in all_dirs
                     if re.search(r"(game|asset|res|data|bundle|content|media|cdn)", d, re.I)]
        if gs2c:
            # у gs2c манифесты лежат в .../desktop/game/ и .../desktop/client/
            near = [d for d in all_dirs if d.endswith("/game") or d.endswith("/client")]
            game_dirs = near + [d for d in game_dirs if d not in near]
        dirs = []
        for d in sorted(game_dirs, key=lambda x: -x.count("/")) + sorted(all_dirs, key=lambda x: -x.count("/")):
            if d not in dirs:
                dirs.append(d)
        dirs = dirs[:args.grid_dirs]
        log("каталоги для зонда: %s" % ", ".join("/".join(d.rsplit("/", 2)[-2:]) for d in dirs[:4]))
        tpl = ("main_resources%03d.json", "resources%03d.json", "game_resources%03d.json",
               "data%03d.json", "bundle%03d.json", "chunk%03d.json")
        # сначала самые вероятные шаблоны по всем каталогам, потом остальные
        for t in list(tpl[:2]) + list(tpl[2:]):
            for d in dirs:
                for i in range(args.grid_count):
                    grid.append(d + "/" + (t % i))
        log("зонд сетки манифестов: %d кандидатов" % len(grid))
        alive = url_ok_many(grid[:args.grid_probe], args.probe_workers)
        if alive:
            log("сетка дала манифестов: %d" % len(alive))
            urls |= set(alive)
        manifests = sorted(set(manifests) | set(alive))

    # проход 1: скелеты, манифесты и атласы
    first = picked["atlas"] + picked["skel"] + picked["json"] + manifests
    log("проход 1: скачиваю %d файлов…" % len(first))
    download_set(first, root, args.workers, args.timeout, "проход 1")

    # проход 1.5: автономный скан — ссылки из текстов и имён из бинарных бандлов
    if args.scan:
        texts = [u for u in urls
                 if u.lower().split("?")[0].endswith((".js", ".json", ".html", ".txt", ".xml", ".m3", ".manifest"))
                 and not looks_junk(u)][:args.scan_texts]
        packed = [u for u in urls if u.lower().endswith(PACKED_EXT) and not looks_junk(u)][:12]
        log("скан: текстов %d, бинарных бандлов %d" % (len(texts), len(packed)))
        refs = set()
        for u in texts:
            try:
                refs |= text_refs(fetch(u, timeout=30))
            except Exception:                             # noqa: BLE001
                continue
        blob_refs = set()
        for u in packed:
            try:
                blob_refs |= binary_refs(fetch(u, timeout=60))
            except Exception:                             # noqa: BLE001
                continue
        log("скан: ссылок в текстах %d, имён в бандлах %d" % (len(refs), len(blob_refs)))
        bases = bases_of(urls | {u.rsplit("/", 1)[0] + "/x" for u in urls})
        spine_refs = {r for r in (refs | blob_refs)
                      if re.search(r"\.(atlas|skel|scn)\b|spine|skeleton", r, re.I)}
        probes = [u for u in resolve_refs(spine_refs, bases) if not looks_junk(u)]
        log("скан: пробую %d кандидатов" % len(probes))
        alive = url_ok_many(probes[:args.probe_limit], args.probe_workers)
        log("скан: отвечает %d" % len(alive))
        if alive:
            log("скан: скачиваю найденное (%d)" % len(alive))
            download_set(alive, root, args.workers, args.timeout, "скан")
            urls |= set(alive)
            picked = pick_assets(urls, kinds)

    # проход 1.55: ассеты, вшитые в манифесты движка (base64 внутри JSON)
    if args.inline:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from inline_assets import extract as extract_inline
        except Exception:                                 # noqa: BLE001
            extract_inline = None
        if extract_inline:
            added = 0
            for base in (root, os.path.join(root, "_manifests")):
              for dirpath, _dirs, fns in os.walk(base):
                for fn in fns:
                    if not fn.lower().endswith((".json", ".js", ".txt", ".manifest")):
                        continue
                    fp = os.path.join(dirpath, fn)
                    if os.path.getsize(fp) > 60 * 1048576:
                        continue
                    try:
                        with open(fp, encoding="utf-8", errors="replace") as f:
                            payload = f.read()
                    except OSError:
                        continue
                    got = extract_inline(payload)
                    for name, data in got.items():
                        rel = os.path.relpath(fp, root)
                        out_dir = os.path.join(os.path.dirname(rel), "inline")
                        dst = os.path.join(root, out_dir, name)
                        if not os.path.exists(dst):
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            with open(dst, "wb") as out:
                                out.write(data)
                            added += 1
            if added:
                log("инлайн-ассеты из манифестов: +%d файлов" % added)

    # проход 1.6: Spine всегда лежит комплектом — проверяем соседей каждой находки
    seeds = [u for u in urls if re.search(r"\.(json|skel|atlas|scn)$", u, re.I)]
    mates = []
    for u in seeds[:args.probe_limit]:
        stem = re.sub(r"\.(json|skel|atlas|scn)$", "", u, re.I)
        for ext in (".json", ".skel", ".atlas"):
            c = stem + ext
            if c != u and c not in mates:
                mates.append(c)
    mates = [m for m in mates if not looks_junk(m)]
    got_mates = url_ok_many(mates, args.probe_workers)
    if got_mates:
        log("парные файлы Spine: +%d" % len(got_mates))
        download_set(got_mates, root, args.workers, args.timeout, "парные")
        urls |= set(got_mates)

    # проход 2: только те картинки, что реально перечислены в скачанных атласах
    pages, atlas_files = [], []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            fp = os.path.join(dirpath, fn)
            if fn.lower().endswith((".atlas", ".atlas.txt", ".fnt")):
                atlas_files.append(fp)
            elif fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                pages.append(fp)
    have = {os.path.basename(p).lower() for p in pages}
    need_urls = []
    for ap_ in atlas_files:
        try:
            names = atlas_page_refs(open(ap_, encoding="utf-8", errors="replace").read())
        except OSError:
            continue
        base = os.path.dirname(ap_)
        remote = REMOTE.get(ap_, "")
        rdir = remote.rsplit("/", 1)[0] + "/" if remote else ""
        for nm in names:
            if nm.lower() in have:
                continue
            have.add(nm.lower())
            by_name = NAME2URL.get(nm) or NAME2URL.get(nm.rsplit("/", 1)[-1])
            if by_name:
                need_urls.append(by_name)
                continue
            cand = [u for u in urls if u.lower().endswith(nm.lower().split("?")[0])]
            if cand:
                need_urls.append(cand[0])
                continue
            if rdir:                       # страница лежит рядом с атласом на сервере
                need_urls.append(rdir + nm)
            else:
                for b_ in bases_of([base + "/x"])[:6]:
                    need_urls.append(b_ + "/" + nm)
    if kinds & {"png"} and need_urls:
        log("проход 2: страницы атласов %d (из %d скачанных картинок)" % (len(need_urls), len(pages)))
        download_set(need_urls, root, args.workers, args.timeout, "проход 2")
    elif not pages and picked.get("png") and not atlas_files:
        log("атласов нет — беру найденные текстуры напрямую (%d)" % len(picked["png"]))
        download_set(picked["png"][:400], root, args.workers, args.timeout, "текстуры")

    # сборка
    total, files = 0, []
    for dirpath, _dirs, fns in os.walk(root):
        for fn in fns:
            fp = os.path.join(dirpath, fn)
            sz = os.path.getsize(fp)
            total += sz
            files.append((os.path.relpath(fp, root), sz))
    report = {"url": args.url, "engine": info["engine"], "discovered": len(urls),
              "json": len(picked["json"]), "atlas": len(picked["atlas"]),
              "skel": len(picked["skel"]), "files": len(files), "bytes": total,
              "seconds": round(time.time() - started, 1)}
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, _sz in files:
            z.write(os.path.join(root, rel), rel)
        z.writestr("fetch-report.json", json.dumps(report, ensure_ascii=False, indent=1))
    if MANIFESTS:
        log("манифестов отдельно: %d" % len(MANIFESTS))
    if REJECTED:
        kinds = {}
        for _u, why in REJECTED:
            kinds[why] = kinds.get(why, 0) + 1
        log("отброшено мусорных ответов: %d (%s)" % (
            len(REJECTED), ", ".join("%s×%d" % (k, v) for k, v in sorted(kinds.items()))))
    log("готово: %d файлов, %.1f МБ за %ss" % (len(files), total / 1048576, report["seconds"]))
    if total > limit:
        log("ВНИМАНИЕ: архив больше лимита %d МБ" % args.max_mb)
    if not files:
        log("НИЧЕГО НЕ НАЙДЕНО: движок «%s» — возможно, это не Spine-игра или нужен вход на сайт" % info["engine"])
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
