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
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".ktx", ".pvr", ".avif")
SPINE_EXT = (".json", ".atlas", ".atlas.txt")
DEFAULT_KINDS = ("json", "atlas", "png")


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
        from urllib.parse import urlparse
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
GAME_EXT = (".skel", ".atlas", ".atlas.txt", ".json", ".png", ".jpg", ".jpeg",
            ".webp", ".ktx", ".data", ".unityweb", ".bundle", ".pck", ".zip",
            ".bin", ".mp3", ".ogg", ".fnt", ".txt")


def find_chrome() -> str:
    import shutil
    for c in CHROME_CANDIDATES:
        p = shutil.which(c) if not c.startswith("/") else (c if os.path.exists(c) else None)
        if p:
            return p
    return ""


def capture_urls(url: str, netlog: str, budget_ms: int = 25000) -> list:
    """Гоняет страницу в headless Chrome и собирает все запрошенные URL."""
    chrome = find_chrome()
    if not chrome:
        return []
    profile = os.path.join(os.path.dirname(netlog), "chrome-profile")
    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
           "--disable-extensions", "--mute-audio", "--hide-scrollbars",
           f"--user-data-dir={profile}",
           f"--virtual-time-budget={budget_ms}",
           f"--log-net-log={netlog}", url]
    try:
        subprocess.run(cmd, timeout=budget_ms / 1000 + 90,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:                          # noqa: BLE001
        pass
    urls = set()
    try:
        with open(netlog, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return []
    for m in re.finditer(r'"url":\s*"([^"]+)"', raw):
        u = m.group(1).replace("\\/", "/")
        if u.startswith(("http://", "https://")):
            urls.add(u)
    urls = {u.split("?")[0].split("#")[0] for u in urls}
    return harvest_iframes(urls, netlog)


CHROME_CHROME = ("/img/", "/assets/img/", "/css/", "/fonts/", "/favicon",
                 "/static/img/", "/media/logos", "/images/logo", "google",
                 "googletagmanager", "recaptcha", "facebook", "anjcdn",
                 "doubleclick", "cookie", "/site.webmanifest")


def harvest_iframes(urls: set, netlog: str) -> list:
    """Повторяет захват для iframe-страниц — там обычно и лежит сама игра."""
    pages = set()
    for u in urls:
        if re.search(r"/(game|games|play|portal|launch|casino)/", u) and \
                not any(c in u for c in CHROME_CHROME):
            pages.add(u)
    extra = set()
    for p in list(pages)[:2]:
        tmp = netlog + ".2"
        extra |= set(capture_urls(p, tmp, budget_ms=20000))
    return sorted(urls | extra)


def harvest(url: str, netlog: str, kinds) -> list:
    """Отбирает из захваченных URL игровые ассеты нужных видов."""
    exts = []
    if "json" in kinds:
        exts += [".json", ".data", ".unityweb", ".bundle"]
    if "atlas" in kinds:
        exts += [".atlas", ".atlas.txt", ".fnt"]
    if "png" in kinds:
        exts += [".png", ".jpg", ".jpeg", ".webp", ".ktx"]
    out, seen = [], set()
    for u in capture_urls(url, netlog):
        if any(c in u for c in CHROME_CHROME) and not u.lower().endswith((".atlas", ".skel")):
            continue
        low = u.lower()
        for e in GAME_EXT:
            if low.endswith(e):
                kind_ok = (e in (".skel",) or e in exts or
                           (e == ".json" and "json" in kinds) or
                           (e in (".mp3", ".ogg", ".bin", ".zip", ".pck") and False))
                if kind_ok and u not in seen:
                    seen.add(u)
                    out.append(u)
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Скачать ассеты Spine по ссылке запуска игры")
    ap.add_argument("url", help="ссылка запуска, напр. https://host/launch?key=..&gameName=..")
    ap.add_argument("-o", "--out", default="input.zip")
    ap.add_argument("--max-mb", type=int, default=300)
    ap.add_argument("--kinds", default=",".join(DEFAULT_KINDS))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=1200)
    args = ap.parse_args()
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}

    t0 = time.time()
    base, launcher, _html = parse_launch(args.url)
    log(f"fetch: base={base}")
    if not launcher:
        log("fetch: не найден launcher.js — это не страница запуска?")
        return 2
    log(f"fetch: launcher={launcher}")
    js = fetch(launcher, timeout=120).decode("utf-8", "replace")
    pairs = parse_manifest(js)
    log(f"fetch: записей в манифесте: {len(pairs)}")
    first_kinds = {k for k in kinds if k in ("json", "atlas")}
    sel = [(f, n) for f, n in pairs if wanted(n, first_kinds)]
    log(f"fetch: к скачиванию: {len(sel)} (виды: {', '.join(sorted(first_kinds))})")

    total = 0
    buf = {}
    errors = 0

    def download(url, timeout=60):
        return fetch(url, timeout=timeout)

    def one(item):
        files, name = item
        url = base + files
        try:
            data = fetch(url, timeout=args.timeout // 6 or 60)
            return name, data
        except Exception as e:               # noqa: BLE001
            return name, f"ERR {type(e).__name__}"

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, (name, data) in enumerate(ex.map(one, sel), 1):
            if isinstance(data, str):
                errors += 1
                if errors <= 5:
                    log(f"fetch: не скачан {name}: {data}")
                continue
            buf[name] = data
            total += len(data)
            if i % 50 == 0:
                log(f"fetch: {i}/{len(sel)} … {total/1048576:.1f} МБ")
            if total > args.max_mb * 1048576:
                log(f"fetch: превышен лимит {args.max_mb} МБ — останавливаюсь")
                break

    if not buf:
        log("fetch: ничего не скачалось")
        return 3

    # второй проход: страницы атласов (иначе нет текстур → нет превью)
    if "png" in kinds and buf:
        # манифест: ключ (папка манифеста, имя файла) -> хеш-путь
        by_dir_name = {}
        for files, logical in pairs:
            key = (os.path.dirname(logical), os.path.basename(logical))
            by_dir_name.setdefault(key, files)
        pages = []          # (куда положить, откуда качать)
        for name, data in list(buf.items()):
            if not name.lower().endswith((".atlas", ".atlas.txt")):
                continue
            adir = os.path.dirname(name)
            try:
                text = data.decode("utf-8", "replace")
            except Exception:                # noqa: BLE001
                continue
            for line in text.split("\n"):
                s2 = line.strip()
                if not s2 or ":" in s2:
                    continue
                if not s2.lower().endswith(IMAGE_EXT):
                    continue
                page = os.path.basename(s2)
                target = os.path.join(adir, page)
                if target in buf:
                    continue
                files = by_dir_name.get((adir, page))
                if files is None:
                    continue
                pages.append((target, files))
        log(f"fetch: страниц атласов к скачиванию: {len(pages)}")

        def one_img(item):
            target, files = item
            try:
                return target, fetch(base + files, timeout=60)
            except Exception as e:           # noqa: BLE001
                return target, f"ERR {type(e).__name__}"

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for target, data in ex.map(one_img, pages):
                if isinstance(data, str):
                    errors += 1
                    continue
                buf[target] = data
                total += len(data)
                if total > args.max_mb * 1048576:
                    log("fetch: лимит размера достигнут во втором проходе")
                    break
        log(f"fetch: после страниц: {len(buf)} файлов, {total/1048576:.1f} МБ")

    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(buf.items()):
            z.writestr(name, data)
    log(f"fetch: готово: {len(buf)} файлов, {total/1048576:.1f} МБ → {args.out} "
        f"({time.time()-t0:.0f} c, ошибок {errors})")
    with open(os.path.splitext(args.out)[0] + "-fetch.txt", "w", encoding="utf-8") as f:
        f.write(f"files={len(buf)}\nbytes={total}\nerrors={errors}\n"
                f"base={base}\nseconds={round(time.time()-t0,1)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
