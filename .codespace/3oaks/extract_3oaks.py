#!/usr/bin/env python3
"""Экстрактор Spine-пар провайдера 3Oaks (betman-demo).

Как устроены игры 3Oaks (проверено на 4_dragon_pearls, 3_superpower_diamonds,
3_super_hot_teapots — три разных движка: goreel/enjoy/hraymo):

  https://3oaks.com/api/v1/games/<game_id>/play?lang=en
      └─ <meta property="og:image" content="https://static.3oaks.com/gs/
          clients_<engine>/<game_id>/<version>/thumbs/en.jpg?v=<rev>">
         из этой ссылки получаем базу ассетов (движок и версия — внутри пути).

  <base>/src/game.js — минифицированный бандл, в нём литеральным списком
  лежат имена скелетов: "bigwin.skel" либо "bigwin.json" (у части игр скелеты
  приходят сразу как Spine-JSON, у части — бинарные .skel). Имена полные:
  в 4_dragon_pearls их 31, и ровно 31 .atlas реально грузит браузер.

  <base>/assets/packs/<вариант>/<имя>.{skel|json,atlas,png}
      варианты: 720 (PNG), 720_webp (WebP), 720_avif (AVIF) — один и тот же
      набор файлов, клиент выбирает по возможностям браузера. Нам нужен 720:
      страницы атласа в нём обычные PNG, которые понимает наш конвейер.
      В game.js это зашито как AssetsResolutions:{prefix:"720",scale:1}.

Никакого браузера: всё определяется по HTML и бандлу. Никаких зашитых имён:
список берётся из кода игры, вариант подбирается пробой, лишние .json
(фрейм-сеты вида bonus-0.json и конфиги частиц bg_*_emitter.json)
отсеиваются проверкой «это Spine-JSON».

Запуск:
  python3 .codespace/3oaks/extract_3oaks.py <play-url> -o <out>
  # или символом/базой:
  python3 .codespace/3oaks/extract_3oaks.py 4_dragon_pearls -o <out>
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TIMEOUT = 45
VARIANTS = ("720", "720_webp", "720_avif")          # PNG → WebP → AVIF
PAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".ktx")
NAME_RE = re.compile(rb'"([A-Za-z0-9_][A-Za-z0-9_.-]{1,60}\.(?:skel|json))"')
OG_RE = re.compile(rb'property="og:image"\s+content="([^"]+)"')
BUNDLES = ("src/game.js", "src/libs.js", "init.js", "src/main.js")

# Диагностика
def say(msg: str) -> None:
    print("[3oaks] %s" % msg, flush=True)


def note(msg: str) -> None:
    print("::notice::3oaks: %s" % msg, flush=True)


def warn(msg: str) -> None:
    print("::warning::3oaks: %s" % msg, flush=True)


def get(url: str, want: int = 1 << 30) -> bytes | None:
    """GET с запасным вариантом на случай временной ошибки сети."""
    last: Exception | None = None
    for attempt in range(2):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = r.read(want)
            return data
        except urllib.error.HTTPError as e:
            if e.code in (403, 404, 410):
                return None
            last = e
        except Exception as e:                       # сеть/таймаут
            last = e
    say("не докачал %s (%s)" % (url, last))
    return None


def resolve_base(arg: str) -> str | None:
    """Из play-URL или символа игры получаем базу ассетов на static.3oaks.com."""
    if "static.3oaks.com" in arg:
        return arg.rstrip("/")
    url = arg if arg.startswith("http") else \
        "https://3oaks.com/api/v1/games/%s/play?lang=en" % arg
    html = get(url)
    if not html:
        warn("не открылась страница игры: %s" % url)
        return None
    m = OG_RE.search(html)
    if not m:
        warn("на странице нет og:image — не понимаю, где ассеты: %s" % url)
        return None
    base = m.group(1).decode("utf-8", "replace").split("?")[0]
    base = re.sub(r"/thumbs/.*$", "", base).rstrip("/")
    if "static.3oaks.com" not in base:
        warn("og:image указывает не на static.3oaks.com: %s" % base)
        return None
    return base


def list_names(base: str) -> list[str]:
    """Имена скелетов из бандлов игры. Список полный — это манифест, не лог."""
    names: set[str] = set()
    for rel in BUNDLES:
        data = get("%s/%s" % (base, rel), want=64 << 20)
        if not data:
            continue
        found = {m.group(1).decode() for m in NAME_RE.finditer(data)}
        if found:
            say("%s: имён в списке %d" % (rel, len(found)))
        for fn in found:
            names.add(os.path.splitext(fn)[0])
        if names and rel == "src/game.js":
            break                            # game.js — главный источник
    return sorted(names)


def is_spine_json(blob: bytes) -> bool:
    """Скелет ли это. У Spine-JSON есть skeleton{spine:...} и bones/slots."""
    head = blob[:200000]
    if b'"skeleton"' not in head:
        return False
    return b'"bones"' in head or b'"slots"' in head


def atlas_pages(atlas_text: str, atlas_dir: str) -> list[str]:
    """Страницы атласа. Строка страницы — имя файла без отступа, дальше size:."""
    lines = [ln.rstrip("\n") for ln in atlas_text.split("\n")]
    out: list[str] = []
    for i, ln in enumerate(lines[:-1]):
        s = ln.strip()
        if not s or s.startswith(" ") or not s.lower().endswith(PAGE_EXT):
            continue
        nxt = lines[i + 1].strip()
        if not (nxt.startswith("size:") and "," in nxt):
            continue
        out.append(s)
    return out


def to_png(path: str) -> bool:
    """WebP/AVIF-страницу переводим в PNG: наш конвейер умеет только PNG/JPEG."""
    if path.lower().endswith((".png", ".jpg", ".jpeg")):
        return True
    try:
        from PIL import Image
    except Exception as e:
        warn("нет Pillow, нечем перевести %s: %s" % (os.path.basename(path), e))
        return False
    try:
        with Image.open(path) as im:
            im.convert("RGBA").save(os.path.splitext(path)[0] + ".png", optimize=True)
    except Exception as e:
        warn("не перевёл %s: %s" % (os.path.basename(path), e))
        return False
    try:
        os.remove(path)
    except OSError:
        pass
    return True


def fetch_one(base: str, name: str, out_root: str,
              sticky: dict) -> tuple[str, str, int, str] | None:
    """Качает одну пару: скелет + атлас + страницы. Возвращает (имя, вид, байт)."""
    order = list(VARIANTS)
    if sticky.get("v"):
        order = [sticky["v"]] + [v for v in order if v != sticky["v"]]
    for var in order:
        pdir = "%s/assets/packs/%s" % (base, var)
        atlas = get("%s/%s.atlas" % (pdir, name), want=8 << 20)
        if not atlas:
            continue
        skel_ext = None
        skel = get("%s/%s.skel" % (pdir, name), want=64 << 20)
        if skel and len(skel) > 128:
            skel_ext = ".skel"
        else:
            js = get("%s/%s.json" % (pdir, name), want=96 << 20)
            if js and len(js) > 128 and is_spine_json(js):
                skel_ext = ".json"
                skel = js
        if not skel_ext:
            continue
        sticky["v"] = var
        dst = os.path.join(out_root, name)
        os.makedirs(dst, exist_ok=True)
        total = 0
        with open(os.path.join(dst, name + skel_ext), "wb") as f:
            f.write(skel)
        total += len(skel)
        with open(os.path.join(dst, name + ".atlas"), "wb") as f:
            f.write(atlas)
        total += len(atlas)
        for page in atlas_pages(atlas.decode("utf-8", "replace"), pdir):
            dest = os.path.join(dst, os.path.basename(page))
            if not os.path.exists(dest) or os.path.getsize(dest) < 256:
                blob = get("%s/%s" % (pdir, page), want=48 << 20)
                if not blob:
                    warn("%s: не докачал страницу %s" % (name, page))
                    continue
                with open(dest, "wb") as f:
                    f.write(blob)
                total += len(blob)
            else:
                total += os.path.getsize(dest)
            if not to_png(dest):
                return None
        # страницы лежат рядом со скелетом, атлас их ищет по имени — ок
        return (name, skel_ext, total, var)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("game", help="play-URL 3oaks.com или символ игры")
    ap.add_argument("-o", "--out", default="./3oaks-out", help="каталог выхода")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--base", default="", help="готовая база ассетов (для отладки)")
    args = ap.parse_args()

    base = args.base or resolve_base(args.game)
    if not base:
        return 1
    say("база: %s" % base)
    names = list_names(base)
    if not names:
        warn("в бандлах нет ни одного имени .skel/.json — нечего качать")
        return 1
    say("кандидатов: %d" % len(names))

    out_root = os.path.join(args.out, "extracted", "spine")
    os.makedirs(out_root, exist_ok=True)
    sticky: dict = {}
    done: list[tuple[str, str, int, str]] = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(fetch_one, base, n, out_root, sticky): n for n in names}
        for fut in cf.as_completed(futs):
            res = fut.result()
            if res is None:
                continue
            done.append(res)
    done.sort()
    for name, ext, size, var in done:
        say("  %-32s %s %7.1f МБ  %s" % (name, ext, size / 1048576.0, var))
    n_skel = sum(1 for _ in done)
    say("ГОТОВО: пар %d, найдено имён %d" % (n_skel, len(names)))
    if n_skel:
        note("%s: Spine-пар %d" % (os.path.basename(base), n_skel))
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"base": base, "candidates": len(names), "pairs": n_skel,
                   "items": [{"name": n, "ext": e, "bytes": b, "variant": v}
                             for n, e, b, v in done]}, f, ensure_ascii=False, indent=1)
    return 0 if n_skel else 1


if __name__ == "__main__":
    sys.exit(main())
