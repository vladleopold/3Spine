#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Прямой захват игр PG Soft (тип html5Game.do), без браузера и без прокси.
#
# Почему так: лобби отдаёт HTML, в котором лежит gameConfig с публичным
# datapath. Игра тянет оттуда файлы main_resourcesNNN.json /
# other_resourcesNNN.json, и ВНУТРИ каждого файла текстуры записаны как
# data:image/png;base64 — то есть по URL скачивается json, а картинок нет.
# Здесь мы их распаковываем в настоящие .png.
#
# Использование: python3 .codespace/capture_pgsoft.py <url> <out_dir> [--max-index N]
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import parse_qs, urlparse
from collections import Counter

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0 Safari/537.36"
RESOURCE_SERIES = ("main_resources", "other_resources", "resources", "common")
GAME_SUBDIR = "desktop/game/"


# Публикатор запросов: подставляется в CI, когда хост режет IP раннера.
RELAY = (os.environ.get("FETCH_RELAY") or "").strip()


def _relay_url(url: str) -> str:
    if "{url}" in RELAY:
        return RELAY.replace("{url}", urllib.parse.quote(url, safe=""))
    sep = "&" if "?" in RELAY else "?"
    return RELAY + sep + "url=" + urllib.parse.quote(url, safe="")


def http_get(url: str, timeout: int = 60) -> bytes:
    """Прямой запрос, а при 403/таймауте — через публикатор (воркер)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as direct_err:
        if not RELAY:
            raise
        print("  прямой запрос не прошёл (%s) — иду через публикатор" % direct_err)
        rreq = urllib.request.Request(_relay_url(url), headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(rreq, timeout=timeout + 30) as resp:
            return resp.read()


def http_head_ok(url: str, timeout: int = 20) -> bool:
    """Проверка существования обычным GET с чтением 1 байта.

    HEAD не годится: публикатор (воркер) держит только GET и на HEAD
    отвечает ошибкой, из-за чего все файлы считались отсутствующими.
    """
    for target in ((url, _relay_url(url)) if RELAY else (url,)):
        req = urllib.request.Request(target, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read(1)
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            if 200 <= e.code < 300:
                return True
        except Exception:
            continue
    return False


def extract_game_config(html: str) -> dict:
    m = re.search(r"gameConfig:\s*'(\{.*?\})'\s*[,;]", html, re.S)
    if not m:
        m = re.search(r'gameConfig:\s*"(\{.*?\})"\s*[,;]', html, re.S)
    if not m:
        return {}
    raw = m.group(1)
    raw = raw.replace("\\u003d", "=").replace("\\u0026", "&")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def split_records(raw: bytes):
    """Файл — поток JSON-объектов, разделённых `},\\r\\n{`."""
    text = raw.decode("utf-8", errors="ignore").strip()
    if text.startswith("{"):
        text = text[1:]
    if text.endswith("}"):
        text = text[:-1]
    for chunk in re.split(r"\}\s*,\s*\{", text):
        chunk = chunk.strip().strip(",").strip()
        if not chunk:
            continue
        try:
            yield json.loads("{" + chunk + "}")
        except json.JSONDecodeError:
            continue


def save_inline_textures(raw: bytes, out_dir: str, name: str, manifest: list, stats: Counter):
    images_dir = os.path.join(out_dir, "images")
    parts_dir = os.path.join(out_dir, "json")
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(parts_dir, exist_ok=True)
    for i, rec in enumerate(split_records(raw)):
        rtype = str(rec.get("type", "Unknown"))
        stats["records"] += 1
        stats["type:" + rtype] += 1
        rid = str(rec.get("id") or ("%s_%d" % (name, i)))
        data = rec.get("data")
        if isinstance(data, str) and data.startswith("data:"):
            head, _, b64 = data.partition(",")
            ext = head.split("/")[-1].split(";")[0] or "png"
            fname = "%s.%s" % (rid, "png" if ext in ("png", "jpeg", "jpg") else ext)
            path = os.path.join(images_dir, fname)
            try:
                blob = base64.b64decode(b64, validate=False)
            except Exception:
                stats["bad_base64"] += 1
                continue
            with open(path, "wb") as fh:
                fh.write(blob)
            stats["images"] += 1
            manifest.append({"source": name, "id": rid, "type": rtype, "file": "images/" + fname, "bytes": len(blob)})
        else:
            # всё прочее (скелеты, атласы, конфиги) сохраняем как есть
            out = {k: v for k, v in rec.items() if k != "data"}
            fname = "%s.%s.json" % (rid, rtype.lower())
            with open(os.path.join(parts_dir, fname), "w", encoding="utf-8") as fh:
                json.dump(out, fh, ensure_ascii=False)
            stats["parts"] += 1
            manifest.append({"source": name, "id": rid, "type": rtype, "file": "json/" + fname})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--max-index", type=int, default=60)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    stats = Counter()
    manifest: list = []

    # 1) пробуем лобби — там лежит gameConfig с datapath
    html = ""
    try:
        print("Загружаю лобби: %s" % args.url)
        html = http_get(args.url, timeout=90).decode("utf-8", errors="ignore")
        with open(os.path.join(args.out, "lobby.html"), "w", encoding="utf-8") as fh:
            fh.write(html)
    except Exception as e:
        # лобби может отдавать 403 по IP раннера; datapath тогда строим из symbol
        print("лобби недоступен (%s) — строим datapath из symbol" % e)

    cfg = extract_game_config(html) if html else {}
    datapath = (cfg.get("datapath") or "").rstrip("/")

    if not datapath:
        qs = parse_qs(urlparse(args.url).query)
        symbol = (qs.get("symbol") or [""])[0]
        if not symbol:
            print("не нашли ни datapath, ни symbol — захват невозможен")
            return 2
        p = urlparse(args.url)
        datapath = "%s://%s/gs2c/common/v3/games-html5/games/vs/%s" % (p.scheme, p.netloc, symbol)
        print("datapath собран из symbol=%s: %s" % (symbol, datapath))
    else:
        print("datapath: %s" % datapath)
    if cfg.get("mgckey"):
        print("в конфиге есть mgckey (сессия), для статики он не нужен")

    base = datapath + "/" + GAME_SUBDIR
    found = []
    for series in RESOURCE_SERIES:
        for i in range(args.max_index + 1):
            name = "%s%03d.json" % (series, i)
            url = base + name
            if not http_head_ok(url):
                continue
            try:
                raw = http_get(url, timeout=120)
            except Exception as e:
                print("  %s: не скачался (%s)" % (name, e))
                continue
            with open(os.path.join(args.out, name), "wb") as fh:
                fh.write(raw)
            before = stats["images"]
            save_inline_textures(raw, args.out, name, manifest, stats)
            print("  %s: %d КБ, картинок +%d" % (name, len(raw) // 1024, stats["images"] - before))
            found.append(name)

    # рядом могут лежать скелеты/атласы отдельными файлами
    for extra in ("game.js", "index.html", "config.json", "settings.json", "version.json"):
        if http_head_ok(base + extra):
            blob = http_get(base + extra)
            with open(os.path.join(args.out, extra), "wb") as fh:
                fh.write(blob)
            print("  + %s (%d КБ)" % (extra, len(blob) // 1024))
            found.append(extra)

    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"datapath": datapath, "files": found,
                   "counts": dict(stats), "records": manifest}, fh, ensure_ascii=False, indent=1)

    print("Итого: файлов ресурсов %d, записей %d, картинок %d, прочих частей %d"
          % (len(found), stats["records"], stats["images"], stats["parts"]))
    if stats["images"] == 0:
        print("ВАЖНО: inline-текстур не найдено — формат файлов другой, нужен разбор")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
