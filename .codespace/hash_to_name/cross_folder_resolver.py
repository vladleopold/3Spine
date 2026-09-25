#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cross_folder_resolver.py
========================
Утилита для поиска файлов-ресурсов в параллельных папках.

Решает проблему: atlas/skeleton в папке A, а PNG/JSON — в папке B (сиблинге).

Используется из:
  - restore_names_cli.py  (build_image_rename_map)
  - unpack_atlases.py     (extra_image_dirs)
  - spine_project_packager_gui.py (tool_unpack_atlases, tool_restore_original_names)
"""

import os
import re
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Определение "корня ресурсов"
# ─────────────────────────────────────────────────────────────────────────────

# Признаки папки-контейнера ресурсов (res/, assets/, resources/, public/)
_RESOURCE_ROOT_NAMES = {
    'res', 'assets', 'resources', 'public', 'static',
    'www', 'data', 'files', 'game', 'content',
}

# Паттерн для папок типа game-main1, game-main6 и т.п.
_NUMBERED_DIR_RE = re.compile(r'^(.+?)[-_]?(\d+)$')


def find_resource_root(path: Path, max_levels: int = 5) -> Path:
    """
    Поднимается вверх от path ища папку-контейнер ресурсов (res/, assets/ и т.п.).
    Возвращает ту папку или родителя path если не нашли.
    """
    current = path if path.is_dir() else path.parent
    for _ in range(max_levels):
        if current.name.lower() in _RESOURCE_ROOT_NAMES:
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return path.parent if path.is_dir() else path.parent.parent


def get_sibling_dirs(src_path: Path, max_depth: int = 2) -> list[Path]:
    """
    Возвращает список директорий-сиблингов для src_path.

    Стратегия:
      1. Сиблинги в той же родительской папке
      2. Все подпапки общего resource-root (res/, assets/ и т.п.)
      3. Для папок типа game-main6 — другие game-mainN
    """
    result: list[Path] = []
    seen: set[Path] = {src_path}

    # --- 1. Прямые сиблинги (одинаковый родитель) ---
    parent = src_path.parent
    if parent.exists():
        for sibling in sorted(parent.iterdir()):
            if sibling.is_dir() and sibling not in seen:
                result.append(sibling)
                seen.add(sibling)

    # --- 2. Resource root siblings ---
    resource_root = find_resource_root(src_path)
    if resource_root != parent and resource_root.exists():
        for item in sorted(resource_root.iterdir()):
            if item.is_dir() and item not in seen:
                result.append(item)
                seen.add(item)
            # Подпапки resource root (res/game-main6 → res/)
            if item.is_dir():
                for sub in sorted(item.iterdir()):
                    if sub.is_dir() and sub not in seen:
                        result.append(sub)
                        seen.add(sub)

    # --- 3. Нумерованные варианты (game-main6 → game-main1..20) ---
    m = _NUMBERED_DIR_RE.match(src_path.name)
    if m:
        base_name, num_str = m.group(1), m.group(2)
        for i in range(1, 30):
            for sep in ('', '-', '_'):
                candidate = parent / f"{base_name}{sep}{i}"
                if candidate.is_dir() and candidate not in seen:
                    result.append(candidate)
                    seen.add(candidate)

    return result


def find_file_in_dirs(
    filename: str,
    search_dirs: list[Path],
    also_try_exts: tuple = ('.png', '.jpg', '.webp', '.avif', '.jpeg'),
) -> Path | None:
    """
    Ищет файл filename в списке директорий.
    Если не нашёл с точным расширением — пробует альтернативные.

    Возвращает Path к найденному файлу или None.
    """
    base, ext = os.path.splitext(filename)
    for d in search_dirs:
        if not d.is_dir():
            continue
        # Точное совпадение
        candidate = d / filename
        if candidate.exists():
            return candidate
        # Альтернативные расширения (png ↔ avif ↔ webp)
        if ext.lower() in also_try_exts:
            for alt_ext in also_try_exts:
                if alt_ext == ext.lower():
                    continue
                alt = d / (base + alt_ext)
                if alt.exists():
                    return alt
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Публичный API для atlas/image поиска
# ─────────────────────────────────────────────────────────────────────────────

def collect_image_search_dirs(src_path: Path) -> list[Path]:
    """
    Собирает все директории где могут находиться текстуры для атласов из src_path.

    Возвращает упорядоченный список (src_path первый).
    """
    dirs = [src_path]
    siblings = get_sibling_dirs(src_path)

    # Добавляем только те сиблинги где есть изображения
    img_exts = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.avif', '.tiff', '.tif', '.gif'}
    for s in siblings:
        if s in dirs:
            continue
        try:
            if any(f.suffix.lower() in img_exts for f in s.iterdir() if f.is_file()):
                dirs.append(s)
        except PermissionError:
            pass

    return dirs


def collect_atlas_search_dirs(src_path: Path) -> list[Path]:
    """
    Собирает все директории где могут находиться atlas-файлы.
    """
    dirs = [src_path]
    siblings = get_sibling_dirs(src_path)

    atlas_exts = {'.atlas', '.txt'}
    for s in siblings:
        if s in dirs:
            continue
        try:
            has_atlas = any(
                f.suffix.lower() in atlas_exts or f.name.lower().endswith('.atlas.txt')
                for f in s.iterdir() if f.is_file()
            )
            if has_atlas:
                dirs.append(s)
        except PermissionError:
            pass

    return dirs


def build_cross_folder_image_map(src_path: Path) -> dict[str, str]:
    """
    Строит карту {hashed_image_filename → real_filename} из ВСЕХ атласов,
    включая атласы из параллельных папок.

    Ключ — имя файла с хэшем (basename), значение — реальное имя.
    """
    from restore_names_cli import is_hashed_name, strip_short_hash

    rename_map: dict[str, str] = {}
    img_exts = {'.png', '.webp', '.jpg', '.jpeg', '.bmp', '.avif', '.tiff', '.tif', '.gif'}

    # Папки с атласами
    atlas_dirs = collect_atlas_search_dirs(src_path)
    # Папки с изображениями
    image_dirs = collect_image_search_dirs(src_path)

    for atlas_dir in atlas_dirs:
        try:
            atlases = list(atlas_dir.glob('*.atlas')) + list(atlas_dir.glob('*.atlas.txt'))
        except Exception:
            continue
        for atlas_path in atlases:
            try:
                real_page = None
                for line in atlas_path.read_text(encoding='utf-8', errors='ignore').splitlines():
                    line = line.strip()
                    if line and not line.startswith('#') and '.' in line and ':' not in line and not line.startswith(' '):
                        real_page = line
                        break
                if not real_page:
                    continue

                # Файл с реальным именем уже существует — пропускаем
                if (atlas_path.parent / real_page).exists():
                    continue

                real_ext = os.path.splitext(real_page)[1].lower()
                if real_ext not in img_exts:
                    continue

                # Ищем хэш-файл во всех image_dirs
                for img_dir in image_dirs:
                    for candidate in img_dir.glob('*' + real_ext):
                        if is_hashed_name(candidate.stem):
                            rename_map[candidate.name] = real_page
            except Exception:
                pass

    return rename_map
