#!/usr/bin/env python3
"""Собирает previews/index.json для сайта по Spine-парам экстрактора.

Сайт рисует по карточке на каждую пару и качает её поштучно, поэтому:
  * страница атласа переносится в previews/<имя>.png — это и есть «скриншот»;
  * рядом кладётся <имя>.spine, чтобы карточка умела качать пару целиком.
"""
import json
import os
import shutil
import sys


def build(root: str) -> int:
    """Собирает карточки по парам из каталогов spine/ и spine_projects/.

    Раскладка может быть любой: и с подпапками на пару (spine/<имя>/…),
    и плоской (spine/<имя>.json рядом с <имя>.atlas) — группируем по имени.
    """
    prev = os.path.join(root, "previews")
    stems: dict[str, dict] = {}

    for sub in ("spine", "spine_projects"):
        base_dir = os.path.join(root, sub)
        if not os.path.isdir(base_dir):
            continue
        for dirpath, _dirnames, filenames in os.walk(base_dir):
            for fn in filenames:
                stem, ext = os.path.splitext(fn)
                rec = stems.setdefault(stem, {})
                if ext in (".json", ".atlas", ".png", ".jpg"):
                    rec.setdefault(ext, os.path.join(dirpath, fn))

    if not stems:
        return 0

    os.makedirs(prev, exist_ok=True)
    items = []
    for name in sorted(stems):
        rec = stems[name]
        if ".json" not in rec or not os.path.isfile(rec[".json"]):
            continue
        page = None
        for ext in (".png", ".jpg"):
            if ext in rec and os.path.isfile(rec[ext]):
                page = "previews/%s%s" % (name, ext)
                shutil.move(rec[ext], os.path.join(root, page))
                break
        if page is None:
            continue
        with open(rec[".json"], encoding="utf-8", errors="ignore") as f:
            head = f.read(8192)
        spine = "previews/%s.spine" % name
        with open(os.path.join(root, spine), "w", encoding="utf-8") as f:
            f.write(head if head.lstrip().startswith("{") else "{}")
        items.append({
            "png": page,
            "spine": spine,
            "name": name,
            "bytes": os.path.getsize(os.path.join(root, page)),
            "kind": "atlas",
        })
    with open(os.path.join(prev, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=1)
    return len(items)


if __name__ == "__main__":
    for root in sys.argv[1:]:
        n = build(root)
        if n:
            print("previews/index.json для %s: %d анимаций" % (root, n))
