#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# БЛОК-РАСПАКОВКИ: после переименования (если был включён hash-путь)
# распаковывает атласы (*.atlas, *.atlas.txt, *.json atlas) в папку /images
# и раскладывает картинки по подпапкам. Работает, когда атласы/спрайты лежат
# рядом с json, в подпапке или в параллельных папках.
# Использование: python3 unpack_block.py <input.zip> <output.zip>
import os
import sys
import shutil
import tempfile
import zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from safezip import safe_unzip

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from unpack_atlases import unpack  # noqa: E402


def unzip(zin_path: str, dst: str) -> None:
    """Безопасная распаковка: safezip отсекает "..", абсолютные пути и бомбы."""
    return safe_unzip(zin_path, dst)


def rezip(src: str, zout_path: str) -> None:
    with zipfile.ZipFile(zout_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(src):
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, src))


def main() -> None:
    zin, zout = sys.argv[1], sys.argv[2]
    tmp = tempfile.mkdtemp()
    loglines = []
    try:
        src = os.path.join(tmp, "in")
        os.makedirs(src)
        unzip(zin, src)

        out_img = os.path.join(src, "images")
        os.makedirs(out_img, exist_ok=True)
        count, strategies = unpack(src, output=out_img)
        loglines.append(f"unpack-block: распаковано {count} картинок в /images")
        for path, strat in sorted(strategies.items()):
            loglines.append(f"  {os.path.relpath(path, src)} -> {strat}")
        print(f"unpack-block: распаковано {count} картинок в/папку {out_img}")

        with open(os.path.join(src, "unpack-log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(loglines) + "\n")

        rezip(src, zout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()