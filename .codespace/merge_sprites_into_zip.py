#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Мержит распакованные спрайты в скомпилированный архив.
# Входы пишутся параллельно (Spine-компиляция и распаковка спрайтов),
# поэтому каждый работает со своей копией, а здесь мы их соединяем.
#
# Использование: python3 .codespace/merge_sprites_into_zip.py <compile.zip> <sprites_dir> <output.zip>
import os
import shutil
import sys
import zipfile


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(
            "нужны аргументы: merge_sprites_into_zip.py <compile.zip> <sprites_dir> <output.zip>")
    src = os.path.abspath(sys.argv[1])
    sprites = os.path.abspath(sys.argv[2])
    dst = os.path.abspath(sys.argv[3])

    if not os.path.isdir(sprites) or not any(files for _r, _d, files in os.walk(sprites)):
        shutil.copyfile(src, dst)
        print("спрайтов нет (каталог пуст) — архив без изменений: %s" % os.path.basename(dst))
        return

    added = 0
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
        for root, _dirs, files in os.walk(sprites):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, sprites).split(os.sep)
                arc = os.path.join(*rel) if rel[0] == "images" else os.path.join("images", *rel)
                zout.write(full, arc)
                added += 1
    print("спрайтов добавлено в архив: %d" % added)


if __name__ == "__main__":
    main()
