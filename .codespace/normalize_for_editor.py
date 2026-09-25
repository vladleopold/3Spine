#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
normalize_for_editor.py
=======================
Готовит Spine JSON 3.x к импорту редактором.

Зачем: нативный C++ конвертер пишет кривую как `"curve": 0.25` и иногда
без полей c2/c3/c4 (например `{"curve": 0.25, "c3": 0.75}`). Редактор,
читающий 3.8-данные, такую запись не принимает:
"Error reading animation: … Invalid curve".

Правила (только для skeleton.spine 2.x/3.x):
  1. "curve": [c1, c2, c3, c4]   (формат 4.x) → curve=c1, c2=c2, c3=c3, c4=c4
  2. "curve": <число> без части c2/c3/c4        → дописываем недостающие
                                                  (c2=0.0, c3=0.75, c4=1.0)
  3. "curve": "stepped"                          → без изменений
  4. полная 3.8-форма (curve + c2 + c3 + c4)    → без изменений

Использование: normalize_for_editor.py <file-or-dir> [...]
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

DEFAULT_C2 = 0.0
DEFAULT_C3 = 0.75
DEFAULT_C4 = 1.0


def fix_frame(frame: Any) -> int:
    """Починить кривую в одном кадре таймлайна. Возвращает число правок."""
    if not isinstance(frame, dict) or "curve" not in frame:
        return 0
    curve = frame["curve"]

    if isinstance(curve, list):
        # 4.x: [c1, c2, c3, c4] → 3.8: curve/c2/c3/c4
        if len(curve) == 4 and all(isinstance(v, (int, float)) for v in curve):
            frame["curve"], frame["c2"], frame["c3"], frame["c4"] = curve
            return 1
        # неизвестная форма — safest: убрать
        frame.pop("curve", None)
        for k in ("c2", "c3", "c4"):
            frame.pop(k, None)
        return 1

    if isinstance(curve, (int, float)) and not isinstance(curve, bool):
        added = 0
        if "c2" not in frame:
            frame["c2"] = DEFAULT_C2
            added += 1
        if "c3" not in frame:
            frame["c3"] = DEFAULT_C3
            added += 1
        if "c4" not in frame:
            frame["c4"] = DEFAULT_C4
            added += 1
        return added

    return 0


def walk(node: Any) -> int:
    changed = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("rotate", "scale", "shear", "translate", "color", "deform",
                       "drawOrder", "attachment", "event", "positions", "spacings",
                       "mix", "curves", "offsets"):
                if isinstance(value, list):
                    for frame in value:
                        changed += fix_frame(frame)
                else:
                    changed += walk(value)
            else:
                changed += walk(value)
    elif isinstance(node, list):
        for item in node:
            changed += fix_frame(item) if isinstance(item, dict) else walk(item)
    return changed


def normalize_file(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return 0
    skel = data.get("skeleton")
    version = str(skel.get("spine", "") or "") if isinstance(skel, dict) else ""
    if version and not version.startswith(("2.", "3.")):
        return 0
    changed = walk(data.get("animations", {}))
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
                        total += normalize_file(os.path.join(root, n))
                        files += 1
        elif t.lower().endswith(".json"):
            total += normalize_file(t)
            files += 1
    print(f"normalize: файлов {files}, кривых приведено к формату 3.8: {total}")


if __name__ == "__main__":
    main()
