#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
manifest_resolver.py
====================
Резолвер имён по манифестам игр (Cocos/Unity-подобные сборки).

В манифестах вида launcher.<hash>.js / settings.json лежат пары:

    "files":"res/game-main3/<sha256>.json",
    "path" :"game:res/spine/background/background.json"

где `files` — реальный путь файла на диске (с хеш-именем), а `path` —
логический (оригинальный) путь ресурса. Это самый надёжный источник имён:
он покрывает ВСЕ хешированные файлы сборки, включая svg/mp3/xml, которые
невозможно вывести из содержимого.

Публичный API:
    build_manifest_map(root)            -> dict[hash_basename] = real_basename
    apply_manifest_names(root, dry_run) -> (renamed, details)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Tuple

HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# "files":"<path>" ... "path":"<scheme>:<real path>"  (в любом порядке, в пределах объекта)
PAIR_RE = re.compile(
    r'"files"\s*:\s*"([^"]+)"[\s\S]{0,400}?"path"\s*:\s*"([^"]+)"'
)
PAIR_REV_RE = re.compile(
    r'"path"\s*:\s*"([^"]+)"[\s\S]{0,400}?"files"\s*:\s*"([^"]+)"'
)

TEXT_EXT = {".js", ".json", ".txt", ".xml", ".plist", ".html", ".cfg", ".ini"}


def _real_basename(logical: str) -> str:
    """Из 'game:res/spine/coin/coin.json' достаём 'coin.json'."""
    name = logical.split(":", 1)[-1]
    name = name.replace("\\", "/").rstrip("/")
    base = name.split("/")[-1]
    return base


def build_manifest_map(root) -> Dict[str, str]:
    """Собрать карту хеш-имя → реальное имя по всем манифестам в дереве."""
    root = Path(root)
    mapping: Dict[str, str] = {}

    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in TEXT_EXT:
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    data = f.read()
            except OSError:
                continue
            if '"files"' not in data or '"path"' not in data:
                continue
            for hashed, logical in PAIR_RE.findall(data):
                _put(mapping, hashed, logical)
            for logical, hashed in PAIR_REV_RE.findall(data):
                _put(mapping, hashed, logical)
    return mapping


def _put(mapping: Dict[str, str], hashed_path: str, logical_path: str) -> None:
    hashed_base = os.path.basename(hashed_path.replace("\\", "/"))
    stem = os.path.splitext(hashed_base)[0]
    if not HASH_RE.match(stem):
        return
    real = _real_basename(logical_path)
    if not real or real == hashed_base:
        return
    real_stem = os.path.splitext(real)[0]
    if HASH_RE.match(real_stem):
        return
    mapping.setdefault(hashed_base, real)


def _unique_target(directory: Path, desired: str) -> str:
    """Не перетирать существующие файлы: coin.png → coin_2.png."""
    if not (directory / desired).exists():
        return desired
    stem, ext = os.path.splitext(desired)
    i = 2
    while (directory / f"{stem}_{i}{ext}").exists():
        i += 1
    return f"{stem}_{i}{ext}"


def apply_manifest_names(root, dry_run: bool = False) -> Tuple[int, List[str]]:
    """Переименовать все файлы, чьё реальное имя известно из манифеста."""
    root = Path(root)
    mapping = build_manifest_map(root)
    renamed = 0
    details: List[str] = []

    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for fn in sorted(files):
            real = mapping.get(fn)
            if not real:
                continue
            src = Path(dirpath) / fn
            dst_name = real if not dry_run else _unique_target(src.parent, real)
            if dry_run:
                renamed += 1
                details.append(f"{os.path.relpath(src, root)} -> {dst_name}")
                continue
            dst_name = _unique_target(src.parent, dst_name)
            try:
                os.rename(src, src.parent / dst_name)
            except OSError as e:
                details.append(f"FAIL {fn} -> {dst_name}: {e}")
                continue
            renamed += 1
            details.append(f"{os.path.relpath(src, root)} -> {dst_name}")

    return renamed, details


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Переименование по манифестам игр")
    ap.add_argument("root")
    ap.add_argument("--apply", action="store_true", help="применить (иначе dry-run)")
    args = ap.parse_args()

    n, lines = apply_manifest_names(args.root, dry_run=not args.apply)
    print(f"манифест: переименовано {n} файлов")
    for line in lines[:200]:
        print("  " + line)
