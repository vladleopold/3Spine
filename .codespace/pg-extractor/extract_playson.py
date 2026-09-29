#!/usr/bin/env python3
"""Экстрактор Spine-пар для игр Playson (box-int / xplatform).

У Playson другая архитектура, чем у PragmaticPlay UHT:

  1. страница запуска содержит <base href="https://<host>/<path>/games/<game>/">
  2. index.html по этому base — шаблон; в нём подключаются
     launcher.<hash>.js и game.<hash>.js
  3. game.<hash>.js содержит встроенный манифест ресурсов:
        {"<package>": {"site": ..., "group": ..., "resources": [
            {"folder": {}, "size": N, "files": "res/<pkg>/<hash>.<ext>",
             "path": "<site>:res/spine/<scene>/<name>.<ext>"}, ...]}}
  4. сами файлы лежат по адресу <base>/<files> — без сессии и авторизации
  5. на одну сцену приходится один .atlas + одна страница .png, но сразу
     несколько скелетов .json, поэтому атлас и картинку копируем каждому

Вывод: out/spine/<имя>/<имя>.json + <имя>.atlas + <имя>.png — готовая пара.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0 Safari/537.36"
DELAY = float(os.environ.get("FETCH_DELAY") or "0.3")
_last = [0.0]


def _throttle() -> None:
    gap = time.time() - _last[0]
    if gap < DELAY:
        time.sleep(DELAY - gap)
    _last[0] = time.time()


def get(url: str, timeout: int = 60, tries: int = 3) -> bytes | None:
    for _ in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if data:
                return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
        time.sleep(1.0)
    return None


def find_base(url: str) -> str:
    """Базовый адрес игры: из <base href>, иначе из URL."""
    html = (get(url) or b"").decode("utf-8", "ignore")
    m = re.search(r'<base\s+href="([^"]+)"', html, re.I)
    if m:
        return m.group(1).rstrip("/") + "/"
    # запасной путь: /launch?...&gameName=X -> .../games/X/
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    game = (q.get("gameName") or q.get("gamename") or [""])[0]
    if game:
        p = urllib.parse.urlparse(url)
        return "%s://%s/games/%s/" % (p.scheme, p.netloc, game)
    return url


def game_script(base: str) -> str:
    """Имя game.<hash>.js из index.html (он идёт шаблоном)."""
    html = (get(base + "index.html") or b"").decode("utf-8", "ignore")
    m = re.search(r'gamescript\s*:\s*"([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'src="(game\.[0-9a-f]+\.js)"', html)
    if m:
        return m.group(1)
    m = re.search(r'(game\.[0-9a-f]{6,}\.js)', html)
    return m.group(1) if m else ""


def manifest(js: str) -> list[tuple[str, str]]:
    """Пары (files, path) из встроенного манифеста."""
    return [(a, b) for a, b in re.findall(r'"files":"([^"]+)"\s*,\s*"path":"([^"]+)"', js)]


def atlas_pages(atlas: bytes) -> list[tuple[str, int, int]]:
    """Страницы из classic .atlas: [(имя файла, w, h), ...].

    Имя страницы знает только сам атлас — в манифесте у общих листов
    (например power_blast.png) путь другой, поэтому берём его оттуда.
    """
    lines = atlas.decode("utf-8", "ignore").split("\n")
    pages = []
    for i, ln in enumerate(lines[:-1]):
        head = ln.strip()
        nxt_raw = lines[i + 1]
        nxt = nxt_raw.strip()
        # Блок страницы начинается с size: В КОЛОНКЕ 0, а у региона он же с
        # отступом — иначе в res/spine атласах именем страницы считалась
        # строка вида "xy: 2, 10". Плюс имя страницы не содержит ':'.
        if not head or ":" in head:
            continue
        if nxt_raw[:1].isspace() or not nxt.startswith("size:"):
            continue
        try:
            w, h = (int(x) for x in nxt.split(":", 1)[1].split(",")[:2])
        except ValueError:
            continue
        pages.append((head, w, h))
    return pages


def retarget_atlas(atlas: bytes, pages: list[str]) -> bytes:
    """Имена страниц → '../../textures/<имя>': пара в spine/<имя>/, лист в textures/."""
    text = atlas.decode("utf-8", "ignore")
    out = []
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        st = ln.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if st in pages and nxt.startswith("size:"):
            out.append("../../textures/" + st)
            continue
        out.append(ln)
    return "\n".join(out).encode("utf-8")


def spine_kind(data: bytes) -> str | None:
    """'json' | 'skel' | None — формат скелета Spine.

    У Playson файлы .json в пакетах часто содержат БИНАРНЫЙ скелет Spine
    (3.8.x): начинается с хеша и версии, а не с фигурной скобки. Такие
    файлы наш прежний детектор не узнавал, поэтому игры выглядели пустыми.
    """
    head = data[:4096]
    if head.lstrip()[:1] == b"{":
        return "json" if (b'"skeleton"' in head or b'"bones"' in head) else None
    # бинарный: короткий печатный префикс (хеш) + версия вида 3.8.99
    if re.search(rb"3\.\d+\.\d+", head) and head[:24].isascii():
        return "skel"
    return None


def logical(path: str) -> str:
    """'common:res/spine/x/y.atlas' -> 'res/spine/x/y.atlas'."""
    return path.split(":", 1)[1] if ":" in path else path


def manifest_spine_pass(base, recs, spine_root, textures, done, size_by_files):
    """Пары Spine по логическим путям res/spine/ — минуя группировку по папкам.

    Главный build() раскладывает записи по ФИЗИЧЕСКОЙ папке (поле files), а у
    Playson физические имена хешированные и лежат в других папках, чем
    логические (поле path). Из-за этого все 13 проектов из res/spine/
    (fs, win_popup, free_spins_choose_bet, widget_tnt, daily_drops…) терялись:
    их атлас попадал в папку с хешем, где нет ни json, ни png этой сцены.
    Здесь берём путь из path и собираем пару оттуда.
    """
    folders = {}
    for files, path in recs:
        lp = logical(path)
        # логический путь начинается с res/spine/, поэтому ищем вхождением без
        # требования ведущего слэша
        if "res/spine/" not in lp:
            continue
        folder = lp.rsplit("/", 1)[0]
        folders.setdefault(folder, []).append((files, lp))

    # индекс картинок по имени файла — страница может лежать в своей папке
    png_by_name = {}
    for files, path in recs:
        lp = logical(path)
        if lp.endswith(".png"):
            png_by_name.setdefault(lp.rsplit("/", 1)[-1], []).append(
                (files, lp, size_by_files.get(files, 0)))

    print("res/spine: папок в манифесте %d, записей %d"
          % (len(folders), sum(len(v) for v in folders.values())))
    pairs = []
    for folder in sorted(folders):
        items = folders[folder]
        atlases = [f for f, lp in items if lp.endswith(".atlas")]
        if not atlases:
            continue
        print("  res/spine %s: файлов %d, атлас %s"
              % (folder, len(items), atlases[0]))
        atlas_bytes = get(base + atlases[0])
        if not atlas_bytes:
            print("  res/spine %s: не скачался атлас" % folder)
            continue
        page_blobs = []
        found_pages = atlas_pages(atlas_bytes)
        print("  res/spine %s: страниц в атласе %d" % (folder, len(found_pages)))
        for page_name, _w, _h in found_pages:
            cand = png_by_name.get(page_name) or []
            same = [c for c in cand if c[1].rsplit("/", 1)[0] == folder]
            pick = (same or sorted(cand, key=lambda c: -c[2]))[0] if cand else None
            if not pick:
                print("  res/spine %s: страница %s не найдена" % (folder, page_name))
                continue
            blob = get(base + pick[0])
            if blob:
                page_blobs.append((page_name, blob))
        if not page_blobs:
            print("  res/spine %s: не нашлось страниц" % folder)
            continue
        names = []
        for files, lp in sorted(items):
            if not lp.endswith(".json"):
                continue
            name = lp[:-len(".json")].rsplit("/", 1)[-1]
            name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
            if name in done:
                continue
            skel = get(base + files)
            if not skel:
                continue
            kind = spine_kind(skel)
            if not kind:
                continue
            d = spine_root / name
            d.mkdir(parents=True, exist_ok=True)
            (d / (name + "." + kind)).write_bytes(skel)
            for pn, blob in page_blobs:
                (textures / pn).write_bytes(blob)
            (d / (name + ".atlas")).write_bytes(
                retarget_atlas(atlas_bytes, [pn for pn, _ in page_blobs]))
            pairs.append({"name": name, "kind": kind, "atlas_source": atlases[0],
                          "pages": [pn for pn, _ in page_blobs],
                          "skeleton_source": files,
                          "bytes": sum(len(b) for _, b in page_blobs)})
            done.add(name)
            names.append(name)
        if names:
            print("  res/spine %s: %d пар (%s)"
                  % (folder, len(names), ", ".join(names[:6])))
    print("res/spine: добавлено пар %d" % len(pairs))
    return pairs


def build(url: str, out: Path) -> dict:
    base = find_base(url)
    print("Playson: база игры %s" % base)
    script = game_script(base)
    if not script:
        return {"error": "не нашли game.<hash>.js в index.html", "base": base}
    js = (get(base + script, timeout=180) or b"").decode("utf-8", "ignore")
    if not js:
        return {"error": "не скачался " + script, "base": base}
    print("Playson: %s — %d КБ, записей манифеста: %d"
          % (script, len(js) // 1024, js.count('"files"')))

    recs = manifest(js)
    size_by_files = dict(re.findall(r'"size"\s*:\s*(\d+)\s*,\s*"files"\s*:\s*"([^"]+)"', js))
    size_by_files = {v: int(k) for k, v in size_by_files.items()}
    by_dir: dict[str, list[tuple[str, str]]] = {}
    for files, path in recs:
        by_dir.setdefault(files.rsplit("/", 1)[0], []).append((files, path))

    spine_root = out / "spine"
    spine_root.mkdir(parents=True, exist_ok=True)
    textures = out / "textures"
    textures.mkdir(parents=True, exist_ok=True)

    pairs: list[dict] = []
    done: set[str] = set()
    dbg = os.environ.get("PLAYSAYON_DEBUG") == "1"
    folders = [d for d, it in by_dir.items() if any(f.endswith(".atlas") for f, _ in it)]
    if dbg:
        print("Playson: папок в манифесте %d, из них с атласами %d: %s"
              % (len(by_dir), len(folders), folders[:5]))
    for folder in folders:
        items = by_dir[folder]
        atlases = [(f, logical(p)) for f, p in items if f.endswith(".atlas")]
        if not atlases:
            continue
        atlas_files, atlas_path = atlases[0]
        if dbg:
            print("  папка %s: атлас %s, json %d"
                  % (folder, atlas_path, sum(1 for f, _ in items if f.endswith(".json"))))
        atlas_bytes = get(base + atlas_files)
        if not atlas_bytes:
            print("  %s: не скачался атлас" % folder)
            continue
        # индекс всех картинок манифеста: имя файла -> (files, размер)
        all_png = {}
        for f, p in items:
            lp = logical(p)
            if lp.endswith(".png"):
                all_png.setdefault(lp.rsplit("/", 1)[-1], []).append((f, size_by_files.get(f, 0)))
        page_blobs = []
        for page_name, _w, _h in atlas_pages(atlas_bytes):
            cand = all_png.get(page_name) or []
            if not cand:
                print("  %s: страница %s не найдена в манифесте" % (folder, page_name))
                continue
            # сначала из своей папки, иначе — самая большая
            same = [c for c in cand if c[0].rsplit("/", 1)[0] == folder]
            pick = (same or sorted(cand, key=lambda c: -c[1]))[0]
            blob = get(base + pick[0])
            if blob:
                page_blobs.append((page_name, blob))
        if not page_blobs:
            print("  %s: не нашлось ни одной страницы атласа" % folder)
            continue
        page_name, page = page_blobs[0]
        for fpath, lpath in sorted({(f, logical(p)) for f, p in items
                                    if f.endswith(".json")}):
            fname = fpath.rsplit("/", 1)[-1]
            if not lpath.endswith(".json"):
                continue
            # читаемое имя из манифеста: res/spine/x/y/widget_s.json -> widget_s
            name = lpath[:-len(".json")].rsplit("/", 1)[-1]
            name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name) or fname[:-len(".json")]
            if name in done:
                continue
            skel = get(base + fpath)
            if not skel:
                continue
            kind = spine_kind(skel)
            if not kind:
                continue                      # это не скелет, а конфиг игры
            d = spine_root / name
            d.mkdir(parents=True, exist_ok=True)
            (d / (name + "." + kind)).write_bytes(skel)
            # Общие листы кладём ОДИН раз в textures/, а в атласе правим строку
            # страницы на относительный путь. Иначе 39 пар × 5 листов дают
            # 167 МБ архива, и GitHub его не примет (лимит 100 МБ).
            for pn, blob in page_blobs:
                (textures / pn).write_bytes(blob)
            (d / (name + ".atlas")).write_bytes(
                retarget_atlas(atlas_bytes, [pn for pn, _ in page_blobs]))
            pairs.append({"name": name, "kind": kind,
                          "atlas_source": atlas_files,
                          "pages": [pn for pn, _ in page_blobs],
                          "skeleton_source": fpath, "bytes": len(page)})
            done.add(name)
            print("  KEEP: spine/%s/%s.%s + .atlas → textures/%s"
                  % (name, name, kind, ", ".join(pn for pn, _ in page_blobs)))

    # Отдельный проход по манифесту. Логический путь (res/spine/...) и физический
    # (files) у Playson не совпадают: физические имена хешированные и лежат в
    # других папках, поэтому группировка по папкам из files теряет целые проекты
    # — например все 13 UI-проектов из res/spine/ (fs, win_popup, widget_tnt…).
    # Здесь идём напрямую по логическим путям, минуя эвристику.
    extra_pairs = manifest_spine_pass(base, recs, spine_root, textures, done, size_by_files)
    pairs.extend(extra_pairs)

    # Манифест в том же виде, что ждёт hash_to_name/manifest_resolver.py:
    # пары "files"/"path" из launcher.<hash>.js. Он построит карту
    # хеш → читаемое имя и переименует файлы сам.
    with open(out / "playson-manifest.json", "w", encoding="utf-8") as f:
        for files, path in recs:
            f.write('{"files":"%s","path":"%s","size":%d},\n'
                    % (files, path, size_by_files.get(files, 0)))
    print("Playson: манифест сохранён (%d записей) — hash_to_name переименует хеши"
          % len(recs))

    with open(out / "playson-report.json", "w", encoding="utf-8") as f:
        json.dump({"base": base, "script": script, "pairs": pairs}, f,
                  ensure_ascii=False, indent=1)
    print("Итого: Playson-пар %d" % len(pairs))
    return {"base": base, "script": script, "pairs": len(pairs)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Playson Spine extractor")
    ap.add_argument("url")
    ap.add_argument("-o", "--out", type=Path, default=Path("./playson-out"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    res = build(args.url, args.out)
    if res.get("error"):
        print("Playson: %s" % res["error"], file=sys.stderr)
        return 1
    if not res.get("pairs"):
        print("Playson: Spine-пар не найдено", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
