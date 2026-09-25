#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skeleton_router.py
==================
Анализатор-детектор входных файлов + выбор пути конвертации.

Классификация:
    text-json     — валидный Spine JSON (текст)              → без конвертации
    binary-clean  — бинарный Spine, U+FFFD = 0, PSON нет    → нативный C++ конвертер
    binary-corrupt— бинарный Spine с U+FFFD / маркером PSON → Spine Restore Tool
    unknown       — не скелет                               → копируется как есть

Публичный API:
    classify(path) -> dict(kind, fffd, pson, version, size, reason)
    engine_order(kind) -> ["native", "restore-tool"] | ["restore-tool", "native"]
"""
from __future__ import annotations

import os
import re
from typing import Dict, List

FFFD = b"\xef\xbf\xbd"
PSON_RE = re.compile(rb'"PSON"')
VERSION_RE = re.compile(rb"\d+\.\d+(?:\.\d+)?")
SPINE_EXTS = (".skel", ".json", ".skel.bytes")


def _read_head(path: str, size: int = 8192) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except OSError:
        return b""


def _whole_file(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def classify(path: str) -> Dict:
    ext = os.path.splitext(path)[1].lower()
    result = {
        "kind": "unknown",
        "fffd": 0,
        "pson": False,
        "version": "",
        "size": 0,
        "reason": "",
    }
    try:
        result["size"] = os.path.getsize(path)
    except OSError:
        return result

    head = _read_head(path)
    if not head:
        result["reason"] = "файл не читается"
        return result

    if head.startswith((b"{", b"[")):
        # текстовый JSON: либо Spine JSON, либо посторонний манифест
        try:
            import json
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("skeleton"), dict) \
                    and data["skeleton"].get("spine"):
                result["kind"] = "text-json"
                result["version"] = str(data["skeleton"].get("spine", ""))
                result["reason"] = "Spine JSON (текст)"
                return result
            result["reason"] = "JSON без skeleton.spine"
            return result
        except Exception:
            result["reason"] = "невалидный JSON"
            return result

    # дальше — бинарные данные
    blob = head
    if result["size"] <= 8 * 1024 * 1024:
        blob = _whole_file(path)
    result["fffd"] = blob.count(FFFD)
    result["pson"] = bool(PSON_RE.search(head))

    m = VERSION_RE.search(head)
    if m:
        result["version"] = m.group(0).decode("ascii", "ignore")

    is_binary_spine = bool(m) and m.start() >= 2 and 1 <= head[m.start() - 1] <= 32
    if not is_binary_spine:
        result["reason"] = "не распознан как бинарный Spine"
        return result

    if result["fffd"] or result["pson"]:
        result["kind"] = "binary-corrupt"
        bits = []
        if result["fffd"]:
            bits.append(f"{result['fffd']} байт U+FFFD")
        if result["pson"]:
            bits.append("маркер PSON")
        result["reason"] = "битый бинарник: " + ", ".join(bits)
        return result

    result["kind"] = "binary-clean"
    result["reason"] = f"бинарный Spine {result['version'] or '?'} (чистый)"
    return result


def engine_order(kind: str, version: str = "") -> List[str]:
    """Порядок движков для данного класса файла.

    binary-corrupt (U+FFFD / PSON) → Spine Restore Tool первым: нативный C++
    на таком материале заведомо не даёт результата.
    binary 4.x                      → ни одного конвертера: формат читает сам
                                      редактор Spine (ЭТАП 1 compile-блока),
                                      оба наших движка рассчитаны на 3.x.
    иначе                           → нативный C++, затем Restore Tool.
    Пустой список означает «передать редактору».
    """
    if kind == "binary-corrupt":
        return ["restore-tool", "native"]
    if kind == "binary-clean" and version.startswith("4."):
        return []
    return ["native", "restore-tool"]


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        info = classify(arg)
        print(f"{info['kind']:16} {info['size']:9d} Б  fffd={info['fffd']:<6} "
              f"version={info['version'] or '-':8} {info['reason']}  {arg}")
