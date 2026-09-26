#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Игра отдаёт ресурсы по хеш-именам, а в атласах страницы указаны логическими
# именами (symbols.png). Маппинг hash -> logical path лежит в манифесте внутри
# launcher.js / json. Скрипт строит алиасы (логическое имя -> реальный файл)
# внутри дерева, чтобы unpack_atlases.py нашёл текстуры по имени.
#
# Использование: python3 .codespace/resolve_sprite_names.py <корень> [каталог_алиасов]
import os
import re
import shutil
import sys
from collections import defaultdict

# "files":"res/game-main/<hash>.png","path":"game:res/spine/symbols/symbols.png"
PAIR = re.compile(
    r'"files"\s*:\s*"([^"]+)"\s*,\s*"path"\s*:\s*"game:([^"]+)"'
)
TEXT_EXT = ('.js', '.json', '.txt', '.html', '.mjs', '.map')
IMG_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.avif')


def find_manifest_pairs(root: str):
    pairs = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ('.git', '__pycache__', 'unpacked')]
        for fn in filenames:
            if not fn.lower().endswith(TEXT_EXT):
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > 80 * 1024 * 1024:
                    continue
                with open(full, 'r', encoding='utf-8', errors='ignore') as fh:
                    text = fh.read()
            except OSError:
                continue
            if '"path":"game:' not in text and '"path": "game:' not in text:
                continue
            for m in PAIR.finditer(text):
                pairs.append((m.group(1), m.group(2)))
    return pairs


def index_files(root: str):
    """basename -> полный путь (первый найденный)."""
    index = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ('.git', '__pycache__', 'unpacked')]
        for fn in filenames:
            index.setdefault(fn, os.path.join(dirpath, fn))
    return index


def main() -> None:
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else '.')
    alias_dir = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(root, '_sprite_aliases')

    pairs = find_manifest_pairs(root)
    if not pairs:
        print('resolve-sprite-names: в манифестах нет пар files->path, алиасы не нужны')
        return

    by_name = defaultdict(list)
    for hashed, logical in pairs:
        name = os.path.basename(logical)
        if name.lower().endswith(IMG_EXT):
            by_name[name].append(os.path.basename(hashed))

    index = index_files(root)
    os.makedirs(alias_dir, exist_ok=True)

    made = 0
    missing = []
    for name, hashed_names in by_name.items():
        target = None
        for h in hashed_names:
            if h in index:
                target = index[h]
                break
        if not target:
            missing.append(name)
            continue
        link = os.path.join(alias_dir, name)
        if os.path.exists(link):
            continue
        try:
            os.link(target, link)
        except OSError:
            shutil.copyfile(target, link)
        made += 1

    print('resolve-sprite-names: пар в манифесте %d, уникальных имён %d, алиасов создано %d, не найдено %d'
          % (len(pairs), len(by_name), made, len(missing)))
    if missing:
        print('  не найдены файлы для: ' + ', '.join(sorted(missing)[:10]))
    print('  каталог алиасов: ' + alias_dir)


if __name__ == '__main__':
    main()
