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


SKEEL_EXTS = (".json", ".skel", ".scn")


def ensure_image_dirs(src: str) -> int:
    """В каждом каталоге со скелетом должен быть images/ с подпапками.

    Требование заказчика: рядом с каждым spine-файлом — каталог images,
    внутри — подпапка на каждый атлас (даже если регионов нет).
    """
    made = 0
    for root, _dirs, files in os.walk(src):
        if os.path.basename(root) == "images":
            continue
        names = [f for f in files if f.lower().endswith(SKEEL_EXTS)]
        atlases = [f for f in files if f.lower().endswith((".atlas", ".atlas.txt", ".fnt"))]
        if not names and not atlases:
            continue
        img_dir = os.path.join(root, "images")
        os.makedirs(img_dir, exist_ok=True)
        made += 1
        subs = []
        for a in atlases:
            sub = os.path.splitext(a)[0]
            subs.append(sub)
        if not subs:
            subs = [os.path.splitext(n)[0] for n in names[:3]]
        for sub in subs:
            subdir = os.path.join(img_dir, sub)
            try:
                os.makedirs(subdir, exist_ok=True)
            except OSError:
                continue
            if not os.listdir(subdir):
                filled = _place_page_image(root, subdir, sub)
                if not filled:
                    # пустая папка исчезла бы из архива — оставляем маркер
                    with open(os.path.join(subdir, ".keep"), "w", encoding="utf-8") as f:
                        f.write("page image not found for atlas %s\n" % sub)
    return made


def _place_page_image(root: str, subdir: str, sub: str) -> bool:
    """Кладёт рядом с регионами исходную страницу атласа (если регионов нет)."""
    atlas = None
    for cand in (sub + ".atlas", sub + ".atlas.txt", sub + ".fnt"):
        p = os.path.join(root, cand)
        if os.path.exists(p):
            atlas = p
            break
    if not atlas:
        return False
    try:
        with open(atlas, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return False
    first = head.strip().split("\n")[0].strip()
    for name in [first, sub]:
        if not name:
            continue
        base = os.path.splitext(name)[0]
        for ext in (".png", ".jpg", ".jpeg", ".webp", ".avif"):
            src_img = os.path.join(root, base + ext)
            if os.path.exists(src_img) and os.path.getsize(src_img) > 64:
                dst = os.path.join(subdir, base + ext)
                try:
                    shutil.copy2(src_img, dst)
                    return True
                except OSError:
                    return False
    return False


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
        made = ensure_image_dirs(src)
        loglines.append(f"unpack-block: распаковано {count} картинок в /images")
        loglines.append(f"unpack-block: каталогов images: {made}")
        for path, strat in sorted(strategies.items()):
            loglines.append(f"  {os.path.relpath(path, src)} -> {strat}")
        print(f"unpack-block: распаковано {count} картинок, каталогов images: {made}")

        with open(os.path.join(src, "unpack-log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(loglines) + "\n")

        rezip(src, zout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()