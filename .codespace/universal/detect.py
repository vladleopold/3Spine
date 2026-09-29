#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Универсальный детектор Spine-ассетов.

Здесь нет знаний ни об одном провайдере: только признаки самих файлов.
Любой набор скачанных файлов (папка, zip, ответы сети) скармливается
`assemble()`, и на выходе получаются готовые проекты `spine/<имя>/`:
`atlas` + `skel|json` + страницы картинок.

Что именно умеет определять:
  * текстовый Spine-атлас (`.atlas`, `.atlas.txt`) и JSON-атлас (Phaser/и т.п.);
  * скелет Spine-JSON (по форме: bones/slots/skins + skeleton);
  * бинарный скелет `.skel` (версия читается из заголовка);
  * страницы картинок, объявленные атласом, — где бы они ни лежали в дереве;
  * общий пул регионов: скелет, которому не хватает картинок, добирает их из
    чужих атласов того же набора (страница копируется в папку проекта).
"""
from __future__ import annotations

import json
import os
import re

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".ktx", ".avif", ".pvr")
SKELETON_BIN_EXT = (".skel", ".bin", ".atlasbin")
ATLAS_EXT = (".atlas", ".atlas.txt", ".atlas.json")
SKELETON_JSON_EXT = (".json",)


# ────────────────────────────── атласы ──────────────────────────────

def _page_names(lines: list[str]) -> list[str]:
    """Имена страниц атласа — по СТРУКТУРЕ, а не по расширению.

    В формате Spine страница лежит в колонке 0, а регион и его свойства —
    с отступом. Расширение у региона может быть любым (`img/1.5x.png`),
    поэтому прежняя проверка «строка кончается на .png» путала регион со
    страницей: в проект попадала лишняя «страница», а настоящая страница
    уезжала в `missing`.
    """
    pages: list[str] = []
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        if ln[:1] in (" ", "\t", "\ufeff"):
            continue                                  # вложенная строка = регион
        s = ln.strip()
        if s.endswith(":") or re.match(r"^[A-Za-z_]+\s*:", s):
            continue
        for nxt in lines[i + 1:i + 4]:
            t = nxt.strip()
            if t.startswith("size:"):
                if re.match(r"^size:\s*\d+\s*,\s*\d+\s*$", t):
                    pages.append(s.replace("\\", "/").split("/")[-1])
                break
            if t.startswith("xy:"):
                continue
            break
    return pages


def looks_like_atlas(text: str) -> bool:
    """Строгая проверка: есть хотя бы одна страница (колонка 0 + `size: w, h`)."""
    return bool(_page_names(text.splitlines()))


def parse_atlas(text: str) -> list[str]:
    """Имена страниц из текстового атласа, в порядке объявления."""
    return _page_names(text.splitlines())


def json_atlas_to_text(obj: dict) -> str | None:
    """JSON-атлас (frames/meta) → текстовый Spine-атлас. None, если не похоже."""
    frames = obj.get("frames")
    if not isinstance(frames, dict) or not frames:
        return None
    if not any(k in obj for k in ("meta", "animations")):
        return None
    meta = obj.get("meta") or {}
    page = meta.get("image") or next(iter(frames.values())).get("filename", "")
    if not page:
        return None
    out = [page.split("/")[-1]]
    out += ["size: 0, 0", "format: RGBA8888", "filter: Linear, Linear", "repeat: none"]
    for name, fr in frames.items():
        f = fr.get("frame", {})
        x, y = int(f.get("x", 0)), int(f.get("y", 0))
        w, h = int(f.get("w", 0)), int(f.get("h", 0))
        out += [name, f"  bounds: {x}, {y}, {x + w}, {y + h}"]
    return "\n".join(out) + "\n"


# ───────────────────────────── скелеты ─────────────────────────────

_SKELETON_KEYS = ("bones", "slots", "ik", "transform", "path", "skin", "skins")


def looks_like_spine_json(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    if not any(k in obj for k in ("bones", "slots")):
        return False
    if "skeleton" in obj or "animations" in obj or "skins" in obj or "skin" in obj:
        return True
    return sum(1 for k in _SKELETON_KEYS if k in obj) >= 2


def skeleton_version(obj) -> str | None:
    sk = obj.get("skeleton") if isinstance(obj, dict) else None
    if isinstance(sk, dict):
        v = sk.get("spine")
        if isinstance(v, str) and re.match(r"^\d+\.\d+", v):
            return v
    return None


_VER_RE = re.compile(r"(?<![\d.])(\d{1,2}\.\d{1,2}(?:\.\d{1,3})?)(?![\d.])")


def binary_skeleton_version(head: bytes) -> str | None:
    """Версия из заголовка бинарного скелета (строка после 8-байтового хеша)."""
    m = _VER_RE.search(head[8:40].decode("latin-1", "ignore"))
    return m.group(1) if m else None


def is_binary_skeleton(head: bytes) -> bool:
    if len(head) < 16 or head[:8] == b"{":
        return False
    return binary_skeleton_version(head) is not None


# ─────────────────────── какие регионы нужны скелету ───────────────────────

def _region_name(raw: str) -> str:
    name = str(raw).replace("\\", "/").split("/")[-1]
    for ext in IMG_EXT:
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    if name.startswith("s_"):
        name = name[2:]
    return name


def skeleton_regions(obj: dict) -> set[str]:
    """Имена регионов, на которые ссылается скелет.

    У вложений без поля `image` (меши и спрайты PragmaticPlay) регионом
    является имя вложения — иначе такие скелеты рисуются без картинок.
    """
    out: set[str] = set()
    skins = obj.get("skins")
    if isinstance(skins, dict):
        skins = list(skins.values())
    if not isinstance(skins, list):
        return out
    for skin in skins:
        if not isinstance(skin, dict):
            continue
        slots = skin.get("attachments") or skin
        if not isinstance(slots, dict):
            continue
        for slot_name, atts in slots.items():
            if not isinstance(atts, dict):
                continue
            names = atts.values() if all(isinstance(v, dict) for v in atts.values()) else [atts]
            for att in names:
                if not isinstance(att, dict):
                    continue
                img = att.get("image") or att.get("path")
                if isinstance(img, str) and img:
                    out.add(_region_name(img))
                else:
                    for key in ("name", "imageName"):
                        v = att.get(key)
                        if isinstance(v, str) and v:
                            out.add(_region_name(v))
    return out


def atlas_regions(text: str) -> list[str]:
    """Имена регионов из текстового атласа.

    Строка-свойство (`rotate:`, `xy:`, `size:`, `orig:`, `offset:`, `index:`,
    `bounds:`, `split:`) регионом не является. Регион может называться как
    `папка/имя` — скелеты ссылаются на него и по полному имени, и по базовому,
    поэтому `region_aliases()` отдаёт оба варианта.
    """
    lines = text.splitlines()
    regions: list[str] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.lower().endswith(IMG_EXT) or s.endswith(":"):
            continue
        if re.match(r"^[A-Za-z_]+\s*:\s*\S", s):          # rotate: false, size: 1, 2
            continue
        tail = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if tail.startswith(("bounds:", "rotate:", "xy:")):
            regions.append(s)
    return regions


def region_aliases(name: str) -> set[str]:
    """`папка/имя` → {`папка/имя`, `имя`} (со снятым префиксом `s_`)."""
    out = {_region_name(name), str(name).replace("\\", "/")}
    base = out.pop()
    out.add(base.rsplit("/", 1)[-1])
    return {a for a in out if a}


# ─────────────────────── сборка проектов из мешка файлов ───────────────────────

class Project:
    __slots__ = ("name", "atlas", "skeleton", "skeleton_ext", "pages", "pool_pages",
                 "missing_pages", "missing_regions", "conflicts")

    def __init__(self, name):
        self.name = name
        self.atlas: str = ""
        self.skeleton: str = ""
        self.skeleton_ext: str = ""
        self.pages: list[tuple[str, str]] = []      # (имя страницы, путь)
        self.pool_pages: list[tuple[str, str]] = []
        self.missing_pages: list[str] = []
        self.missing_regions: list[str] = []
        self.conflicts: list[str] = []


def _skeleton_regions_of(path: str, ext: str):
    """Регионы скелета — только для JSON. У бинарного их не прочитать,
    поэтому для `.skel` возвращается пустое множество (без исключения)."""
    if ext not in SKELETON_JSON_EXT:
        return set()
    try:
        obj = json.loads(open(path, "r", encoding="utf-8", errors="ignore").read())
    except Exception:
        return set()
    return skeleton_regions(obj) if looks_like_spine_json(obj) else set()


def _find_page(name: str, home: str, files: dict) -> str | None:
    """Ищем файл страницы. Сначала рядом с атласом, потом — где есть.

    Плоский пул склеивает разные провайдеры, и одноимённые картинки у разных
    игр встречаются. Поэтому сначала смотрим соседние файлы, и только если
    рядом ничего нет — берём единственное совпадение во всём дереве.
    """
    low = name.lower()
    exact = os.path.join(home, name)
    if os.path.isfile(exact):
        return exact
    cands = files.get(low, [])
    if not cands:
        return None
    for c in cands:
        if os.path.dirname(c) == home:
            return c
    return cands[0] if len(cands) == 1 else None


def assemble(root: str, out_spine: str) -> tuple[list[Project], dict]:
    """Собирает `spine/<имя>/` из любого дерева файлов в `root`.

    Возвращает (проекты, отчёт). Проект = атлас + скелет + страницы.
    """
    files: dict[str, list[str]] = {}     # нижний регистр имени -> ВСЕ пути
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            files.setdefault(n.lower(), []).append(os.path.join(dirpath, n))
    first = {low: c[0] for low, c in files.items()}

    # Ключ — (каталог, stem), а не только stem: в плоском пуле две разные
    # игры дают по своему `logo.atlas`, и одна молча затирала другую.
    atlases: dict[tuple[str, str], tuple[str, str, list[str]]] = {}  # (каталог, stem)
    skels: dict[tuple[str, str], tuple[str, str]] = {}
    pool: list[tuple[str, str, set[str]]] = []             # (stem, текст, регионы)

    for low, path in sorted(first.items()):
        stem, ext = os.path.splitext(low)
        try:
            key = (os.path.dirname(path), stem)
            if low.endswith(ATLAS_EXT):
                text = open(path, "r", encoding="utf-8", errors="ignore").read()
                if looks_like_atlas(text):
                    atlases[key] = (path, text, parse_atlas(text))
                elif ext == ".json":
                    try:
                        conv = json_atlas_to_text(json.loads(text))
                    except Exception:
                        conv = None
                    if conv:
                        atlases[key] = (path, conv, parse_atlas(conv))
            elif ext in SKELETON_JSON_EXT:
                with open(path, "rb") as f:
                    head = f.read(4096)
                if head[:1] == b"{" and b'"skeleton"' not in head and b'"bones"' not in head:
                    continue
                try:
                    obj = json.loads(open(path, "r", encoding="utf-8", errors="ignore").read())
                except Exception:
                    obj = None
                if looks_like_spine_json(obj):
                    skels[key] = (path, skeleton_version(obj) or "?")
            elif ext in SKELETON_BIN_EXT:
                with open(path, "rb") as f:
                    head = f.read(48)
                if is_binary_skeleton(head):
                    skels[key] = (path, binary_skeleton_version(head) or "?")
        except Exception as exc:  # единичный битый файл не должен ронять всё
            print("::warning::детектор пропустил %s: %s" % (path, exc))

    for _key, (_path, text, _pages) in sorted(atlases.items()):
        aliases: set[str] = set()
        for rn in atlas_regions(text):
            aliases |= region_aliases(rn)
        pool.append((_key[1], text, aliases))

    # атлас ищется по (каталог, stem), а если не нашлось — по одному stem
    # во всём дереве: игры часто кладут скелет и атлас в разные папки.
    def find_atlas(key):
        if key in atlases:
            return atlases[key]
        same = [v for k, v in atlases.items() if k[1] == key[1]]
        return same[0] if len(same) == 1 else None

    used_names: set[str] = set()
    projects: list[Project] = []
    for key, (skel_path, _ver) in sorted(skels.items()):
        name = os.path.splitext(os.path.basename(skel_path))[0]
        base_name, bump = name, 2
        while name.lower() in used_names:      # два разных скелета с одним stem
            name = "%s_%d" % (base_name, bump)
            bump += 1
        used_names.add(name.lower())
        pr = Project(name)
        pr.skeleton = skel_path
        pr.skeleton_ext = os.path.splitext(skel_path)[1]
        atlas = find_atlas(key)
        if not atlas:
            continue                           # скелет без атласа — не проект
        pr.atlas = atlas[0]
        home = os.path.dirname(atlas[0])
        taken: set[str] = set()
        for page in atlas[2]:
            src = _find_page(page, home, files)
            if not src:
                pr.missing_pages.append(page)
                continue
            if page.lower() in taken:
                pr.conflicts.append(page)      # две разные картинки на одно имя
                continue
            taken.add(page.lower())
            pr.pages.append((page, src))

        # добираем недостающие регионы из чужих атласов того же набора
        need = _skeleton_regions_of(skel_path, pr.skeleton_ext)
        have: set[str] = set()
        for rn in atlas_regions(atlas[1]):
            have |= region_aliases(rn)
        for region in sorted(need - have):
            home_atlas = None
            for pstem, ptext, pregions in pool:
                if region in pregions and pstem != key[1]:
                    home_atlas = ptext
                    break
            if home_atlas is None:
                pr.missing_regions.append(region)     # проект всё равно делаем:
                continue                               # не хватать картинок —
            for page in parse_atlas(home_atlas):       # не повод его выкинуть
                if page.lower() in taken:
                    continue
                src = _find_page(page, home, files)
                if src:
                    taken.add(page.lower())
                    pr.pool_pages.append((page, src))
        projects.append(pr)

    # запись на диск
    for pr in projects:
        d = os.path.join(out_spine, pr.name)
        os.makedirs(d, exist_ok=True)
        shutil_copy(pr.atlas, os.path.join(d, pr.name + ".atlas"))
        sk_dst = os.path.join(d, pr.name + (".skel" if pr.skeleton_ext == ".skel" else ".json"))
        shutil_copy(pr.skeleton, sk_dst)
        # файл кладём под ИМЕНЕМ, объявленным в атласе: иначе на регистр
        # или регистр на файл карточка не грузит страницу целиком
        for page, src in pr.pages + pr.pool_pages:
            shutil_copy(src, os.path.join(d, page))

    report = {
        "файлов": len(files),
        "атласов": len(atlases),
        "скелетов": len(skels),
        "проектов": len(projects),
        "без атласа": sorted({"%s/%s" % k for k in skels} - {"%s/%s" % k for k in atlases}),
        "без страниц": {p.name: p.missing_pages[:6] for p in projects if p.missing_pages},
        "без регионов": {p.name: p.missing_regions[:6] for p in projects if p.missing_regions},
        "конфликты": {p.name: p.conflicts[:6] for p in projects if p.conflicts},
    }
    return projects, report


def verify(spine_dir: str) -> dict:
    """Сверка готового каталога: что атлас обещает, а чего на диске нет.

    Детектор сам себя проверяет, иначе проект с недостающей страницей уехал бы
    дальше по конвейеру и «сломался» бы уже на сайте.
    """
    out: dict[str, dict[str, list[str]]] = {}
    if not os.path.isdir(spine_dir):
        return out
    for name in sorted(os.listdir(spine_dir)):
        d = os.path.join(spine_dir, name)
        if not os.path.isdir(d):
            continue
        have = {f.lower() for f in os.listdir(d)}
        bad: dict[str, list[str]] = {}
        apath = None
        for cand in (name + ".atlas", name + ".atlas.txt"):
            if cand.lower() in have:
                apath = os.path.join(d, cand)
                break
        if not apath:
            bad["без атласа"] = []
        else:
            text = open(apath, "r", encoding="utf-8", errors="ignore").read()
            miss = [p for p in parse_atlas(text) if p.lower() not in have]
            if miss:
                bad["без страниц"] = miss[:6]
        skel = [f for f in have if f.endswith((".skel", ".json"))
                and not f.endswith(".atlas.json")]
        if not skel:
            bad["без скелета"] = []
        if bad:
            out[name] = bad
    return out


def shutil_copy(src: str, dst: str) -> None:
    import shutil
    shutil.copyfile(src, dst)
