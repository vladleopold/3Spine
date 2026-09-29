#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Универсальный экстрактор Spine.

    run.py <ссылка на игру> --out DIR [--pool DIR ...] [--только-детектор]

Идея: ссылка разбирается не «по провайдеру», а по структуре. Несколько
независимых стратегий-охотников запускаются ВСЕ, каждая сама решает, подходит
ли она (по доказательствам, а не по домену), и всё, что наловлено, сваливается
в общий пул. Пул разбирает универсальный детектор `detect.py`: он сам находит
атласы, скелеты и страницы и собирает готовые проекты. Ни одна стратегия не
имеет права исключать другую — если сработали две, берём обе.

Стратегии:
  pg-uht   — пакеты PragmaticPlay (desktop/mobile, series main/game/gui_res)
  3oaks    — бандлы + assets/packs/<вариант>/ на 3oaks-подобных CDN
  bundles  — провайдер-независимый: скрипты страницы → имена .atlas/.json/.skel
  pool     — уже скачанные каталоги (например, выгрузка сейвера)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CODES = os.path.dirname(HERE)
sys.path.insert(0, CODES)

from universal import detect  # noqa: E402

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126 Safari/537.36"

# ────────────────────────────── сеть ──────────────────────────────

def get(url: str, timeout: int = 25) -> bytes | None:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def http_code(url: str, timeout: int = 15) -> int:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


# ─────────────────────── стратегия: 3oaks-подобные CDN ───────────────────────

PACK_DIRS = ("assets/packs/720", "assets/packs/720_webp", "assets/packs",
             "assets/packs/mobile", "assets/skeleton", "spine", "assets/anim",
             "assets/animation", "res/spine", "assets/spine")
BUNDLES = ("src/game.js", "src/main.js", "src/libs.js", "init.js", "game.js",
           "main.js", "assets/game.js", "static/js/game.js", "js/game.js")
ASSET_RE = re.compile(rb"""["']([^"']{1,120}?\.(?:atlas|skel|json|bin))["']""", re.I)


THUMB_DIRS = ("thumb", "thumbs", "thumbnail", "thumbnails", "preview", "screenshot",
              "screenshots", "img", "images", "cover", "poster", "banner")


def og_image_base(page: bytes) -> str | None:
    """База ассетов из og:image — работает, когда движок и версия внутри пути.

    У 3oaks в og:image лежит `.../<версия>/thumbs/en.jpg`, а ассеты — уровнем
    выше, поэтому каталог с картинкой-превью отбрасывается.
    """
    m = re.search(rb'og:image"\s+content="([^"]+)"', page)
    if not m:
        return None
    parts = urllib.parse.urlsplit(m.group(1).decode("utf-8", "ignore")).path.split("/")
    parts = parts[:-1]
    if parts and parts[-1].lower() in THUMB_DIRS:
        parts = parts[:-1]
    p = urllib.parse.urlsplit(m.group(1).decode("utf-8", "ignore"))
    return "%s://%s%s/" % (p.scheme, p.netloc, "/".join(parts))


def probe_static_bases(page_url: str, page: bytes) -> list[str]:
    """Кандидаты-базы: og:image, скрипты страницы, сам URL."""
    base = urllib.parse.urlsplit(page_url)
    root = "%s://%s/" % (base.scheme, base.netloc)
    out: list[str] = []
    b = og_image_base(page)
    if b:
        out.append(b)
    for m in re.finditer(rb"""src=["']([^"']+\.js[^"']*)["']""", page):
        s = urllib.parse.urljoin(page_url, m.group(1).decode("utf-8", "ignore"))
        out.append(s.rsplit("/", 1)[0] + "/")
    out.append(root)
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def strategy_bundles(page_url: str, pool: str, log) -> dict:
    """Провайдер-независимый: имена скелетов/атласов вытаскиваем из бандлов."""
    page = get(page_url) or b""
    bases = probe_static_bases(page_url, page)
    names: set[str] = set()
    for base in bases[:4]:
        if len(names) > 400:
            break
        for b in BUNDLES:
            data = get(base + b)
            if not data or len(data) < 500:
                continue
            for m in ASSET_RE.finditer(data):
                name = m.group(1).decode("utf-8", "ignore")
                name = name.split("?")[0].rsplit("/", 1)[-1]
                if name.lower().endswith((".atlas", ".skel")) or re.search(r"\.json$", name, re.I):
                    names.add(name)
            if names:
                log("bundles: %s + %s → имён %d" % (base, b, len(names)))
                break
    found = 0
    tried = 0
    for base in bases[:4]:
        for d in PACK_DIRS:
            got_here = 0
            for name in sorted(names):
                stem = name.rsplit(".", 1)[0]
                for ext in (".atlas", ".skel", ".json"):
                    tried += 1
                    if tried > 1200:
                        break
                    url = base + d + "/" + stem + ext
                    fn = os.path.join(pool, stem + ext)
                    if os.path.exists(fn):
                        continue
                    data = get(url, timeout=12)
                    if not data or len(data) < 40:
                        continue
                    open(fn, "wb").write(data)
                    found += 1
                    got_here += 1
            if got_here:
                log("bundles: каталог %s%s дал %d файлов" % (base, d, got_here))
                break
    return {"стратегия": "bundles", "имён": len(names), "файлов": found, "подошла": found > 0}


# ─────────────────────── стратегия: PragmaticPlay UHT ───────────────────────

def strategy_pg(page_url: str, work: str, log) -> dict:
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(page_url).query)
    sym = (q.get("gameSymbol") or q.get("symbol") or [""])[0]
    if not sym:
        return {"стратегия": "pg-uht", "подошла": False, "причина": "нет gameSymbol в ссылке"}
    res = os.path.join(work, "pguht-res")
    script = os.path.join(CODES, "pg-extractor", "find-uht-assets.sh")
    if os.path.exists(script):
        env = dict(os.environ, SYMBOL=sym, OUT_DIR=res)
        try:
            subprocess.run(["bash", script, sym], env=env, timeout=2400)
        except Exception as exc:
            log("pg-uht: %s" % exc)
    if not os.path.isdir(res):
        return {"стратегия": "pg-uht", "подошла": False, "причина": "пакеты не скачались"}
    out = os.path.join(work, "pguht-out")
    cmd = [sys.executable, os.path.join(CODES, "pg-extractor", "extract_pragmatic_uht.py"),
           res, "-o", out, "--symbol", sym]
    try:
        subprocess.run(cmd, timeout=1800, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        log("pg-uht: %s" % exc)
    n = len(os.listdir(os.path.join(out, "spine"))) if os.path.isdir(os.path.join(out, "spine")) else 0
    return {"стратегия": "pg-uht", "символ": sym, "проектов": n, "подошла": n > 0}


# ─────────────────────── стратегия: 3oaks ───────────────────────

def strategy_3oaks(page_url: str, work: str, log) -> dict:
    script = os.path.join(CODES, "3oaks", "extract_3oaks.py")
    if not os.path.exists(script):
        return {"стратегия": "3oaks", "подошла": False, "причина": "нет скрипта"}
    out = os.path.join(work, "oaks-out")
    try:
        subprocess.run([sys.executable, script, page_url, "--out", out],
                       timeout=1800, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        log("3oaks: %s" % exc)
    d = os.path.join(out, "extracted", "spine")
    n = len(os.listdir(d)) if os.path.isdir(d) else 0
    return {"стратегия": "3oaks", "проектов": n, "подошла": n > 0}


# ─────────────────────── сборка проектов из пула ───────────────────────

def gather(roots: list[str], pool: str) -> int:
    """Сливает каталоги в один пул, сохраняя имена (в т.ч. разные папки)."""
    n = 0
    for r in roots:
        if not r or not os.path.isdir(r):
            continue
        for dirpath, _d, names in os.walk(r):
            for name in names:
                src = os.path.join(dirpath, name)
                dst = os.path.join(pool, name)
                if os.path.exists(dst) or os.path.getsize(src) < 8:
                    continue
                shutil.copyfile(src, dst)
                n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", required=True)
    ap.add_argument("--pool", action="append", default=[],
                    help="уже скачанный каталог с ассетами (скеплер, ручная выгрузка)")
    ap.add_argument("--только-детектор", action="store_true",
                    help="не запускать стратегии, разобрать только --pool")
    ap.add_argument("--cap-mb", type=int, default=85)
    a = ap.parse_args()

    work = a.out
    pool = os.path.join(work, "pool")
    os.makedirs(pool, exist_ok=True)
    notes: list[str] = []

    def log(msg: str) -> None:
        notes.append(msg)
        print("::notice::универсальный экстрактор: %s" % msg, flush=True)

    reports = []
    if not a.только_детектор:
        for fn in (strategy_pg, strategy_3oaks, strategy_bundles):
            try:
                if fn is strategy_bundles:
                    rep = fn(a.url, pool, log)
                else:
                    rep = fn(a.url, work, log)
            except Exception as exc:
                rep = {"стратегия": fn.__name__, "подошла": False, "причина": str(exc)[:120]}
            reports.append(rep)
            log("итог стратегии: " + json.dumps(rep, ensure_ascii=False))

    roots = [os.path.join(work, "pguht-out", "spine"),
             os.path.join(work, "oaks-out", "extracted", "spine")] + a.pool
    copied = gather(roots, pool)
    log("в пуле %d файлов (скопировано %d)" % (len(os.listdir(pool)), copied))

    projects, report = detect.assemble(pool, os.path.join(work, "extracted", "spine"))
    for rep in reports:
        rep["детектор"] = report["проектов"]
    log("детектор: " + json.dumps(report, ensure_ascii=False)[:500])
    json.dump({"стратегии": reports, "детектор": report, "заметки": notes},
              open(os.path.join(work, "universal-report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    if not projects:
        print("::warning::универсальный детектор не нашёл ни одного Spine-проекта")
        return 1
    print("УНИВЕРСАЛЬНЫЙ ЭКСТРАКТОР: проектов %d, страниц %d"
          % (len(projects), sum(len(p.pages) + len(p.pool_pages) for p in projects)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
