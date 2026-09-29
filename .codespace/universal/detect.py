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

def looks_like_atlas(text: str) -> bool:
    """Строгая проверка: страница (имя с картиночным расширением) + `size: w, h`."""
    lines = text.splitlines()
    if not lines:
        return False
    pages = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.endswith(":"):
            continue
        if not s.lower().endswith(IMG_EXT):
            continue
        for nxt in lines[i + 1:i + 4]:
            t = nxt.strip()
            if t.startswith("size:"):
                if re.match(r"^size:\s*\d+\s*,\s*\d+\s*$", t):
                    pages += 1
                break
            if t.startswith("xy:"):
                continue
            break
    return pages > 0


def parse_atlas(text: str) -> list[str]:
    """Имена страниц из текстового атласа, в порядке объявления."""
    lines = text.splitlines()
    pages: list[str] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.endswith(":") or not s.lower().endswith(IMG_EXT):
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
    __slots__ = ("name", "atlas", "skeleton", "skeleton_ext", "pages", "pool_pages", "missing")

    def __init__(self, name):
        self.name = name
        self.atlas: str = ""
        self.skeleton: str = ""
        self.skeleton_ext: str = ""
        self.pages: list[str] = []
        self.pool_pages: list[str] = []
        self.missing: list[str] = []


def assemble(root: str, out_spine: str, name_hint: str | None = None) -> tuple[list[Project], dict]:
    """Собирает `spine/<имя>/` из любого дерева файлов в `root`.

    Возвращает (проекты, отчёт). Проект = атлас + скелет + страницы.
    """
    files: dict[str, str] = {}           # нижний регистр -> путь
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            files.setdefault(n.lower(), p)

    atlases: dict[str, tuple[str, str, list[str]]] = {}   # stem -> (путь, текст, страницы)
    skels: dict[str, tuple[str, str]] = {}                 # stem -> (путь, версия)
    pool: list[tuple[str, str, set[str]]] = []             # (stem, текст, регионы)

    for low, path in sorted(files.items()):
        stem, ext = os.path.splitext(low)
        try:
            if low.endswith(ATLAS_EXT):
                text = open(path, "r", encoding="utf-8", errors="ignore").read()
                if looks_like_atlas(text):
                    atlases[stem] = (path, text, parse_atlas(text))
                elif ext == ".json":
                    try:
                        conv = json_atlas_to_text(json.loads(text))
                    except Exception:
                        conv = None
                    if conv:
                        atlases[stem] = (path, conv, parse_atlas(conv))
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
                    skels[stem] = (path, skeleton_version(obj) or "?")
            elif ext in SKELETON_BIN_EXT:
                with open(path, "rb") as f:
                    head = f.read(48)
                if is_binary_skeleton(head):
                    skels[stem] = (path, binary_skeleton_version(head) or "?")
        except Exception as exc:  # единичный битый файл не должен ронять всё
            print("::warning::детектор пропустил %s: %s" % (path, exc))

    for stem, (_path, text, _pages) in atlases.items():
        aliases: set[str] = set()
        for rn in atlas_regions(text):
            aliases |= region_aliases(rn)
        pool.append((stem, text, aliases))

    projects: list[Project] = []
    for stem, (skel_path, ver) in sorted(skels.items()):
        pr = Project(os.path.splitext(os.path.basename(skel_path))[0])
        pr.skeleton = skel_path
        pr.skeleton_ext = os.path.splitext(skel_path)[1]
        atlas = atlases.get(stem)
        if not atlas:
            continue                                   # скелет без атласа — не проект
        pr.atlas = atlas[0]
        used = []
        for page in atlas[2]:
            src = files.get(page.lower())
            if src:
                used.append(src)
            else:
                pr.missing.append(page)
        pr.pages = used
        # добираем недостающие регионы из чужих атласов того же набора
        try:
            obj = json.loads(open(skel_path, "r", encoding="utf-8", errors="ignore").read())
            need = skeleton_regions(obj) if looks_like_spine_json(obj) else set()
        except Exception:
            need = set()
        have: set[str] = set()
        for rn in atlas_regions(atlas[1]):
            have |= region_aliases(rn)
        for region in sorted(need - have):
            for pstem, ptext, pregions in pool:
                if region not in pregions:
                    continue
                for page in parse_atlas(ptext):
                    if page in [os.path.basename(u) for u in pr.pages]:
                        continue
                    src = files.get(page.lower())
                    if src and src not in pr.pages:
                        pr.pool_pages.append(src)
                break
            else:
                pr.missing.append(region)
        projects.append(pr)

    # запись на диск
    for pr in projects:
        d = os.path.join(out_spine, pr.name)
        os.makedirs(d, exist_ok=True)
        shutil_copy(pr.atlas, os.path.join(d, os.path.basename(pr.atlas)))
        sk_dst = os.path.join(d, pr.name + (".skel" if pr.skeleton_ext == ".skel" else ".json"))
        shutil_copy(pr.skeleton, sk_dst)
        for i, src in enumerate(pr.pages):
            base = os.path.basename(src)
            shutil_copy(src, os.path.join(d, base))
        for i, src in enumerate(pr.pool_pages):
            base = os.path.basename(src)
            dst = os.path.join(d, base)
            if not os.path.exists(dst):
                shutil_copy(src, dst)

    report = {
        "файлов": len(files),
        "атласов": len(atlases),
        "скелетов": len(skels),
        "проектов": len(projects),
        "без атласа": sorted(set(skels) - set(atlases)),
        "неполных": {p.name: p.missing[:6] for p in projects if p.missing},
    }
    return projects, report


def shutil_copy(src: str, dst: str) -> None:
    import shutil
    shutil.copyfile(src, dst)
