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
    proj = os.path.join(root, "spine")
    if not os.path.isdir(proj):
        return 0
    prev = os.path.join(root, "previews")
    os.makedirs(prev, exist_ok=True)
    items = []
    for name in sorted(os.listdir(proj)):
        d = os.path.join(proj, name)
        js = os.path.join(d, name + ".json")
        if not os.path.isdir(d) or not os.path.isfile(js):
            continue
        page = None
        for ext in (".png", ".jpg"):
            cand = os.path.join(d, name + ext)
            if os.path.isfile(cand):
                page = "previews/%s%s" % (name, ext)
                shutil.move(cand, os.path.join(root, page))
                break
        if page is None:
            continue
        with open(js, encoding="utf-8", errors="ignore") as f:
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
