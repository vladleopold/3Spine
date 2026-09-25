#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Безопасная распаковка zip: отсекает абсолютные пути, "..", симлинки,
# бомбы (лимит записей/байтов/глубины/коэффициента сжатия).
# Использование: from safezip import safe_unzip as unzip
import os
import posixpath
import re
import stat
import zipfile

MAX_TOTAL_BYTES = int(os.environ.get("MAX_UNPACKED", 400 * 1024 * 1024))
MAX_ENTRIES = int(os.environ.get("MAX_FILES", 5000))
MAX_DEPTH = 32
MAX_RATIO = 200
MAX_NAME = 255
MAX_PATH = 1024

_DRIVE = re.compile(r"^[A-Za-z]:")


class UnsafeArchive(ValueError):
    """Архив небезопасен — распаковка прервана."""


def _check_name(name: str) -> str:
    if not name:
        raise UnsafeArchive("пустое имя записи")
    if "\0" in name:
        raise UnsafeArchive("NUL в имени записи: %r" % name)
    if "\\" in name:
        raise UnsafeArchive("обратный слэш в имени: %r" % name)
    if name.startswith("/") or posixpath.isabs(name) or _DRIVE.match(name):
        raise UnsafeArchive("абсолютный путь в архиве: %r" % name)
    if len(name.encode("utf-8", "surrogateescape")) > MAX_PATH:
        raise UnsafeArchive("слишком длинное имя: %d байт" % len(name))
    parts = []
    for seg in name.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise UnsafeArchive("выход за пределы каталога: %r" % name)
        if len(seg.encode("utf-8", "surrogateescape")) > MAX_NAME:
            raise UnsafeArchive("слишком длинный сегмент пути: %r" % seg)
        parts.append(seg)
    if not parts:
        return ""
    if len(parts) > MAX_DEPTH:
        raise UnsafeArchive("глубина вложенности больше %d: %r" % (MAX_DEPTH, name))
    return "/".join(parts)


def _check_kind(info: zipfile.ZipInfo) -> None:
    mode = info.external_attr >> 16
    if not mode:
        return
    fmt = stat.S_IFMT(mode)
    if fmt == 0:
        return
    if stat.S_ISLNK(fmt):
        raise UnsafeArchive("симлинк в архиве: %r" % info.filename)
    if fmt not in (stat.S_IFREG, stat.S_IFDIR):
        raise UnsafeArchive("не-обычный файл в архиве: %r" % info.filename)


def safe_unzip(zin_path: str, dst: str, *, max_total: int = MAX_TOTAL_BYTES,
               max_entries: int = MAX_ENTRIES, max_ratio: int = MAX_RATIO) -> int:
    """Распаковать zin_path в dst. Возвращает число записанных файлов."""
    dst = os.path.realpath(dst)
    os.makedirs(dst, exist_ok=True)
    written = 0
    with zipfile.ZipFile(zin_path) as z:
        infos = z.infolist()
        if len(infos) > max_entries:
            raise UnsafeArchive("записей %d, максимум %d" % (len(infos), max_entries))
        declared, plan = 0, []
        for info in infos:
            rel = _check_name(info.filename)
            _check_kind(info)
            if info.file_size > max_total:
                raise UnsafeArchive("запись больше лимита: %r" % info.filename)
            if max_ratio and info.file_size > 4096:
                ratio = info.file_size / max(1, info.compress_size)
                if ratio > max_ratio:
                    raise UnsafeArchive("коэффициент сжатия %d:1 в %r" % (ratio, info.filename))
            declared += info.file_size
            if declared > max_total:
                raise UnsafeArchive("архив распаковывается больше чем в %d байт" % max_total)
            if rel:
                plan.append((info, rel))
            elif not info.filename.endswith("/"):
                raise UnsafeArchive("запись не даёт пути: %r" % info.filename)
        total = 0
        for info, rel in plan:
            target = os.path.join(dst, *rel.split("/"))
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if os.path.commonpath([dst, os.path.realpath(target)]) != dst:
                raise UnsafeArchive("запись выходит за пределы каталога: %r" % info.filename)
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            with open(target, "wb") as out, z.open(info) as src:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_total:
                        raise UnsafeArchive("архив распаковывается больше %d байт" % max_total)
                    out.write(chunk)
            written += 1
    return written
