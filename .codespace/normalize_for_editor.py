#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
normalize_for_editor.py
=======================
Готовит Spine JSON к импорту редактором.

Зачем: нативный C++ конвертер выдаёт корректный формат 3.8, где кривая
timeline задаётся числом ("curve": 0.25 — stepped). Редактор 4.x, который
используется в CI (3.8.99-редактор недоступен), такой JSON не принимает:
"Error reading animation: … Invalid curve".

Что делаем (только для данных 3.x):
    "curve": <число>      →  "curve": "stepped"
    "curve": [c1x,c1y,c2x,c2y]  → без изменений (уже bezier)

Использование: normalize_for_editor.py <file-or-dir> [...]
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

TIMELINE_KEYS = ("rotate", "scale", "shear", "translate", "color", "deform",
                 "drawOrder", "attachment", "event")


def norm_curves(node: Any) -> int:
    """Рекурсивно заменить числовые curve на 'stepped'. Возвращает число правок."""
    changed = 0
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "curve" and isinstance(v, (int, float)) and not isinstance(v, bool):
                node[k] = "stepped"
                changed += 1
            else:
                changed += norm_curves(v)
    elif isinstance(node, list):
        for item in node:
            changed += norm_curves(item)
    return changed


def normalize_file(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return 0
    skel = data.get("skeleton")
    version = ""
    if isinstance(skel, dict):
        version = str(skel.get("spine", "") or "")
    if version and not version.startswith(("3.", "2.")):
        return 0
    changed = norm_curves(data.get("animations", {}))
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    return changed


def main() -> None:
    targets = sys.argv[1:]
    total = 0
    files = 0
    for t in targets:
        if os.path.isdir(t):
            for root, _dirs, names in os.walk(t):
                for n in names:
                    if n.lower().endswith(".json"):
                        p = os.path.join(root, n)
                        c = normalize_file(p)
                        total += c
                        files += 1
        elif t.lower().endswith(".json"):
            total += normalize_file(t)
            files += 1
    print(f"normalize: файлов {files}, кривых приведено к 'stepped': {total}")


if __name__ == "__main__":
    main()
