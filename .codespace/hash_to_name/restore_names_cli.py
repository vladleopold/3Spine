#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
restore_names_cli.py
====================
Восстанавливает оригинальные имена файлов из хэш-имён (SHA-256 / MD5).

Поддерживаемые форматы:
  • .json  — Spine binary, Spine text, TexturePacker, particle-config, любой JSON
  • .skel  — Spine binary skel
  • .atlas — Spine TextureAtlas
  • .xml   — Spine XML TextureAtlas
  • .png/.jpg/.avif/... — изображения (через atlas-карту или magic-bytes)
  • .mp3/.wav/.ogg/.svg — медиа (через ссылки в JSON)

Использование:
    python3 restore_names_cli.py /path/to/folder [--dry-run] [--no-recursive]
    python3 restore_names_cli.py /path/to/file.json
"""

import sys
import os
import re
import json
import struct
import argparse
import hashlib
from pathlib import Path
from collections import Counter

# ─────────────────────────────────────────────────────────────────────────────
# Конфигурация
# ─────────────────────────────────────────────────────────────────────────────

IMAGE_EXTS = {'.png', '.webp', '.jpg', '.jpeg', '.bmp', '.avif', '.tiff', '.tif', '.gif'}

# Слова которые встречаются в Spine binary и НЕ являются осмысленными именами
SPINE_GENERIC = {
    'root', 'bone', 'default', 'skeleton', 'slot', 'skin',
    'att', 'attachment', 'attachments', 'image', 'images', 'temp', 'tmp',
    'name', 'hash', 'true', 'false', 'start', 'end', 'time', 'color', 'curve',
    'stepped', 'spine', 'version', 'animations', 'events', 'ik', 'transform',
    'null', 'alpha', 'scale', 'speed', 'blend', 'normal', 'additive',
}

MIN_TOKEN_LEN = 4

HASH_RE = re.compile(r'^[0-9a-fA-F]{32}(?:[0-9a-fA-F]{32})?$')


def is_hashed_name(name: str) -> bool:
    """Проверяет, является ли имя файла длинным hex-хешем."""
    base = os.path.splitext(os.path.basename(name))[0]
    return HASH_RE.match(base) is not None


def strip_short_hash(stem: str) -> str:
    """Убирает _XXXXXXXX (8-char hex) суффикс и leading garbage."""
    if not stem:
        return stem
    stem = re.sub(r'[\r\n\t\x00-\x1f]+', '', stem)
    stem = re.sub(r'^(?:\\u[0-9a-fA-F]{4}|u[0-9a-fA-F]{4}|[^a-zA-Z0-9_])+', '', stem)
    stem = re.sub(r'_([0-9a-fA-F]{8})$', '', stem)
    stem = stem.strip()
    stem = re.sub(r'^[0-9a-fA-F]{32,}$', '', stem)
    return stem.rstrip('_')


# ─────────────────────────────────────────────────────────────────────────────
# Magic bytes — определение реального типа файла
# ─────────────────────────────────────────────────────────────────────────────

# Magic bytes для определения реального формата
_MAGIC = {
    b'\x89PNG': '.png',
    b'\xff\xd8\xff': '.jpg',
    b'GIF8': '.gif',
    b'RIFF': '.webp',   # RIFF....WEBP
    b'<svg': '.svg',
    b'<?xm': '.xml',
    b'ID3\x03': '.mp3',
    b'ID3\x04': '.mp3',
    b'\xff\xfb': '.mp3',
    b'\xff\xf3': '.mp3',
    b'\xff\xf2': '.mp3',
}

# AVIF/HEIF magic: bytes 4-8 == 'ftyp' + brand contains 'avif' or 'heic'
def _detect_real_ext(data: bytes, declared_ext: str) -> str:
    """Возвращает реальное расширение файла по magic bytes."""
    if len(data) < 12:
        return declared_ext
    hdr4 = data[:4]
    # AVIF: ftyp box at offset 4
    if data[4:8] == b'ftyp':
        brand = data[8:12]
        if brand in (b'avif', b'avis', b'heic', b'heif', b'mif1'):
            return '.avif'
    # PNG
    if hdr4 == b'\x89PNG':
        return '.png'
    # JPEG
    if data[:3] == b'\xff\xd8\xff':
        return '.jpg'
    # GIF
    if data[:4] in (b'GIF8', b'GIF9'):
        return '.gif'
    # WEBP
    if hdr4 == b'RIFF' and data[8:12] == b'WEBP':
        return '.webp'
    # SVG / XML (text)
    snippet = data[:100].lstrip(b'\xef\xbb\xbf \t\r\n')  # strip BOM
    if snippet.startswith(b'<svg') or snippet.startswith(b'<?xml'):
        return '.svg' if b'svg' in snippet[:50].lower() else '.xml'
    # MP3
    if data[:3] in (b'ID3', ) or data[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'):
        return '.mp3'
    # JSON (text)
    if snippet and snippet[0:1] in (b'{', b'['):
        return '.json'
    return declared_ext


# Форматы которые Spine НЕ ПОНИМАЕТ → конвертируем в PNG
_NON_SPINE_IMAGE_EXTS = {'.webp', '.avif', '.bmp', '.tiff', '.tif', '.gif'}


def convert_to_png(src_path: Path, dst_path: Path) -> bool:
    """
    Конвертирует WEBP/AVIF/BMP/TIFF в PNG.
    Возвращает True если конвертация успешна.
    dst_path должен иметь расширение .png
    """
    try:
        from PIL import Image
        with Image.open(src_path) as img:
            # Сохраняем прозрачность если есть
            if img.mode in ('RGBA', 'LA', 'P'):
                converted = img.convert('RGBA')
            else:
                converted = img.convert('RGB')
            converted.save(dst_path, 'PNG', optimize=False)
        # Verify the output file was actually written and is non-empty.
        if not dst_path.exists() or dst_path.stat().st_size == 0:
            print(f"  ⚠️  Конвертация не записала файл: {dst_path.name}")
            return False
        return True
    except ImportError:
        print("  ⚠️  PIL не установлен, пропуск конвертации")
        return False
    except Exception as e:
        print(f"  ⚠️  Ошибка конвертации {src_path.name}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Garbage detection
# ─────────────────────────────────────────────────────────────────────────────

_UPPER_RUN_RE = re.compile(r'[A-Z]{3,}')


def _looks_like_garbage(tok: str) -> bool:
    """True если токен похож на base64/UUID/random key."""
    if not tok:
        return True
    if '+' in tok or '/' in tok or '=' in tok:
        return True
    upper = sum(1 for c in tok if c.isupper())
    lower = sum(1 for c in tok if c.islower())
    # Смесь upper+lower без _ → скорее base64
    if len(tok) >= 12 and upper >= 5 and lower >= 5 and '_' not in tok:
        return True
    # Три+ заглавных подряд → random key
    if _UPPER_RUN_RE.search(tok):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Binary string extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_bin_tokens(data: bytes) -> list:
    """Извлекает значимые ASCII-токены из бинарных данных."""
    pattern = re.compile(rb'[a-zA-Z][a-zA-Z0-9_/]{3,60}')
    candidates = []
    for m in pattern.finditer(data):
        tok = m.group().decode('ascii', errors='ignore')
        for part in tok.split('/'):
            # Убираем суффиксы ТОЛЬКО типа _00, _001, _0001 (frame numbers с _)
            # НЕ убираем trailing цифры без _ (background_lt1, bb_bg_fx2 — часть имени!)
            base = re.sub(r'_\d+$', '', part)
            base = base.strip('_')
            if not base or len(base) < MIN_TOKEN_LEN:
                continue
            if is_hashed_name(base):
                continue
            if base.lower() in SPINE_GENERIC:
                continue
            if _looks_like_garbage(base):
                continue
            # Фильтр: 6-char hex = цвет CSS (#ffffff → ffffff)
            if re.match(r'^[0-9a-fA-F]{6}$', base):
                continue
            candidates.append(base)
    return candidates


def infer_name_from_binary(data: bytes) -> str | None:
    """Определяет наиболее вероятное имя файла из binary содержимого."""
    candidates = _extract_bin_tokens(data)
    if not candidates:
        return _infer_name_from_binary_relaxed(data)
    counts = Counter(candidates)

    def score(item):
        """Сравнивает кандидатов по частоте, читаемости и длине имени."""
        tok, cnt = item
        return cnt + (2 if '_' in tok else 0) + min(len(tok), 20) / 20.0

    best = max(counts.items(), key=score)[0]
    result = strip_short_hash(best)
    if result and not is_hashed_name(result) and not _looks_like_garbage(result):
        return result
    return _infer_name_from_binary_relaxed(data)


def _infer_name_from_binary_relaxed(data: bytes) -> str | None:
    """Fallback: scan raw bytes for any printable token when strict extraction fails."""
    if not data:
        return None
    pattern = re.compile(rb'[a-zA-Z][a-zA-Z0-9_/]{3,60}')
    candidates = []
    for m in pattern.finditer(data):
        tok = m.group().decode('ascii', errors='ignore')
        for part in tok.split('/'):
            base = re.sub(r'_\d+$', '', part)
            base = base.strip('_')
            if not base or len(base) < MIN_TOKEN_LEN:
                continue
            if is_hashed_name(base):
                continue
            if _looks_like_garbage(base):
                continue
            if re.match(r'^[0-9a-fA-F]{6}$', base):
                continue
            candidates.append(base)
    if not candidates:
        return None
    counts = Counter(candidates)
    best = max(counts.items(), key=lambda item: item[1])[0]
    result = strip_short_hash(best)
    if result and not is_hashed_name(result) and not _looks_like_garbage(result):
        return result
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Format readers
# ─────────────────────────────────────────────────────────────────────────────

def is_spine_binary(data: bytes) -> bool:
    """True если файл — Spine binary (начинается с версии типа '3.8.99')."""
    try:
        ver_len = data[0]
        if 1 <= ver_len <= 20:
            ver = data[1:1 + ver_len].decode('ascii', errors='replace')
            if re.match(r'^\d+\.\d+', ver):
                return True
    except Exception:
        pass
    return False


def read_atlas_name(path: Path) -> str | None:
    """Первая строка .atlas файла — имя PNG страницы."""
    try:
        for line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                stem = os.path.splitext(line)[0]
                stem = strip_short_hash(stem)
                if stem and not is_hashed_name(stem):
                    return stem
    except Exception:
        pass
    return None


def read_xml_atlas_name(path: Path) -> str | None:
    """imagePath из XML TextureAtlas."""
    try:
        content = path.read_text(encoding='utf-8', errors='ignore')
        m = re.search(r'<TextureAtlas\s+imagePath="([^"]+)"', content)
        if m:
            stem = os.path.splitext(os.path.basename(m.group(1)))[0]
            stem = strip_short_hash(stem)
            if stem and not is_hashed_name(stem):
                return stem
    except Exception:
        pass
    return None


def read_json_name(path: Path, data_bytes: bytes) -> str | None:
    """
    Пробует определить имя из JSON файла.
    Поддерживает: TexturePacker, Spine JSON, particle config, любой JSON.
    """
    try:
        data = json.loads(data_bytes.decode('utf-8', errors='ignore'))
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    # 1. TexturePacker: meta.image
    if 'frames' in data and 'meta' in data:
        img = data['meta'].get('image', '')
        if img:
            stem = strip_short_hash(os.path.splitext(os.path.basename(img))[0])
            if stem and not is_hashed_name(stem):
                return stem

    # 2. Spine text JSON: skeleton.name или кости/слоты
    spine_keys = {'skeleton', 'bones', 'slots', 'skins', 'animations'}
    if len(spine_keys & set(data.keys())) >= 2:
        sk_name = data.get('skeleton', {}).get('name', '') if isinstance(data.get('skeleton'), dict) else ''
        if sk_name:
            clean = strip_short_hash(str(sk_name))
            if clean and not is_hashed_name(clean) and clean.lower() not in SPINE_GENERIC:
                return clean
        candidates = []
        for b in data.get('bones', []):
            if isinstance(b, dict):
                bname = re.sub(r'_\d+$', '', b.get('name', '').split('/')[0])
                bname = strip_short_hash(bname)
                if bname and len(bname) >= MIN_TOKEN_LEN and bname.lower() not in SPINE_GENERIC and not _looks_like_garbage(bname):
                    candidates.append(bname)
        if candidates:
            return Counter(candidates).most_common(1)[0][0]

    # 3. Particle config / generic JSON — ищем поле "image", "texture", "file", "name"
    for key in ('image', 'texture', 'file', 'spriteName', 'atlasName', 'animationName'):
        val = data.get(key)
        if isinstance(val, str) and val:
            stem = strip_short_hash(os.path.splitext(os.path.basename(val))[0])
            if stem and len(stem) >= MIN_TOKEN_LEN and not is_hashed_name(stem) and not _looks_like_garbage(stem):
                # Не берём чистые hex-цвета
                if not re.match(r'^[0-9a-fA-F]{3,8}$', stem):
                    return stem

    # 4. Рекурсивный поиск строк с расширением в любых значениях
    def _find_filenames(obj, depth=0):
        """Рекурсивно извлекает вероятные имена файлов из JSON-данных."""
        if depth > 5:
            return []
        results = []
        if isinstance(obj, dict):
            for v in obj.values():
                results.extend(_find_filenames(v, depth + 1))
        elif isinstance(obj, list):
            for v in obj:
                results.extend(_find_filenames(v, depth + 1))
        elif isinstance(obj, str):
            # Строка выглядит как имя файла (не цвет!)
            if re.search(r'\.(png|jpg|json|atlas|skel|mp3|svg)$', obj, re.I):
                stem = strip_short_hash(os.path.splitext(os.path.basename(obj))[0])
                if stem and len(stem) >= MIN_TOKEN_LEN and not is_hashed_name(stem) and not _looks_like_garbage(stem):
                    if not re.match(r'^[0-9a-fA-F]{3,8}$', stem):
                        results.append(stem)
        return results

    found = _find_filenames(data)
    if found:
        return Counter(found).most_common(1)[0][0]

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Atlas image rename map
# ─────────────────────────────────────────────────────────────────────────────

def build_image_rename_map(src_path: Path) -> dict:
    """
    Строит {hashed_image_filename → real_filename} из .atlas файлов.
    Ищет атласы И изображения в параллельных папках тоже.
    """
    rename_map: dict = {}
    img_exts = {'.png', '.webp', '.jpg', '.jpeg', '.bmp', '.avif', '.tiff', '.tif', '.gif'}

    # Собираем все директории где есть атласы
    atlas_search: list[Path] = [src_path]
    # Добавляем сиблинг-директории (параллельные папки рядом)
    parent = src_path.parent
    if parent.exists():
        for sibling in parent.iterdir():
            if sibling.is_dir() and sibling != src_path:
                atlas_search.append(sibling)

    # Собираем все директории где есть изображения
    image_dirs: list[Path] = []
    for d in atlas_search:
        try:
            if any(f.suffix.lower() in img_exts for f in d.iterdir() if f.is_file()):
                image_dirs.append(d)
        except PermissionError:
            pass
    if src_path not in image_dirs:
        image_dirs.insert(0, src_path)

    for atlas_dir in atlas_search:
        try:
            for atlas_path in sorted(set(atlas_dir.glob('*.atlas')) | set(atlas_dir.glob('*.atlas.txt'))):
                try:
                    real_page = None
                    for line in atlas_path.read_text(encoding='utf-8', errors='ignore').splitlines():
                        line = line.strip()
                        if line and not line.startswith('#') and '.' in line and ':' not in line and not line.startswith(' '):
                            real_page = line
                            break
                    if not real_page:
                        continue
                    if (atlas_path.parent / real_page).exists():
                        continue  # уже правильно назван
                    real_ext = os.path.splitext(real_page)[1].lower()
                    if real_ext not in img_exts:
                        continue
                    # Ищем хэш PNG во ВСЕХ image_dirs
                    for img_dir in image_dirs:
                        for candidate in img_dir.glob('*' + real_ext):
                            if is_hashed_name(candidate.stem):
                                rename_map[candidate.name] = real_page
                except Exception:
                    pass
        except Exception:
            pass

    return rename_map


# ─────────────────────────────────────────────────────────────────────────────
# Конфликт-резолюция: если имя уже занято — добавляем _2, _3, ...
# ─────────────────────────────────────────────────────────────────────────────

def resolve_conflict(p: Path, desired_name: str) -> str | None:
    """
    Если desired_name уже существует и это НЕ тот же файл,
    пробуем desired_stem_2.ext, _3, ... до 20.
    Возвращает свободное имя или None если все заняты.
    """
    new_path = p.parent / desired_name
    if not new_path.exists():
        return desired_name
    # Если это тот же файл (по размеру и первым байтам) — пропускаем
    try:
        if new_path.stat().st_size == p.stat().st_size:
            if new_path.read_bytes(64) == p.read_bytes(64):
                return None  # дубль
    except Exception:
        pass
    stem, ext = os.path.splitext(desired_name)
    for i in range(2, 21):
        candidate = f"{stem}_{i}{ext}"
        if not (p.parent / candidate).exists():
            return candidate
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Bundle map: hash→realname из JS/HTML bundle файлов
# ─────────────────────────────────────────────────────────────────────────────

def build_bundle_rename_map(src_path: Path) -> dict:
    """
    Сканирует JS/HTML файлы в родительских папках (до 3 уровней вверх)
    и извлекает карту {hashed_filename → real_filename}.

    Форматы которые понимаем:
      {"files": "res/folder/abc123.json", "path": "game:res/real/name.json"}
      {"src":"res/folder/abc123.png","dest":"images/real_name.png"}
    """
    rename_map = {}  # basename(hash_file) → basename(real_file)

    # Ищем JS/HTML в src_path и до 3 уровней вверх
    search_dirs = [src_path]
    parent = src_path.parent
    for _ in range(3):
        if parent == parent.parent:
            break
        search_dirs.append(parent)
        parent = parent.parent

    for search_dir in search_dirs:
        for bundle_file in search_dir.glob('*.js'):
            try:
                _parse_bundle_file(bundle_file, rename_map)
            except Exception:
                pass
        for bundle_file in search_dir.glob('*.html'):
            try:
                _parse_bundle_file(bundle_file, rename_map)
            except Exception:
                pass

    return rename_map


def _parse_bundle_file(bundle_file: Path, rename_map: dict) -> None:
    """Парсит один bundle файл и заполняет rename_map."""
    # Читаем кусками, чтобы не загружать весь файл в память
    chunk_size = 256 * 1024  # 256 KB
    MAX_TEXT_BUF = 10 * 1024 * 1024  # 10 MB hard cap on accumulated text
    text_buf = ''
    with open(bundle_file, 'r', encoding='utf-8', errors='ignore') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            text_buf += chunk
            # Обрабатываем оверлап на 150 символов
            _extract_bundle_mappings(text_buf, rename_map)
            text_buf = text_buf[-150:]  # оставляем хвост для следующего чанка
            if len(text_buf) > MAX_TEXT_BUF:
                # Should not happen with the overlap trim, but guard anyway.
                text_buf = text_buf[-MAX_TEXT_BUF:]
    if text_buf:
        _extract_bundle_mappings(text_buf, rename_map)


# Паттерны для извлечения маппингов из bundle
_BUNDLE_PATTERNS = [
    # {"files":"res/folder/HASH.ext","path":"scope:res/real/name.ext"}
    re.compile(r'"files"\s*:\s*"([^"]+/([0-9a-fA-F]{32,}\.[^"]+))"[^}]{0,200}"path"\s*:\s*"(?:[^:"]+:)?([^"]+)"'),
    # reverse order: path first, then files
    re.compile(r'"path"\s*:\s*"(?:[^:"]+:)?([^"]+)"[^}]{0,200}"files"\s*:\s*"([^"]+/([0-9a-fA-F]{32,}\.[^"]+))"'),
    # {"src":"...","dest":"..."}  Webpack / Parcel style
    re.compile(r'"src"\s*:\s*"([^"]+/([0-9a-fA-F]{32,}\.[^"]+))"[^}]{0,200}"dest"\s*:\s*"([^"]+)"'),
]


def _extract_bundle_mappings(text: str, rename_map: dict) -> None:
    """Извлекает соответствия hash→realname из фрагмента bundle-файла."""
    # Pattern 1: files=hash, path=real
    for m in re.finditer(_BUNDLE_PATTERNS[0], text):
        hash_path, hash_base, real_path = m.group(1), m.group(2), m.group(3)
        real_base = os.path.basename(real_path)
        if real_base and not is_hashed_name(os.path.splitext(real_base)[0]):
            rename_map[hash_base] = real_base
    # Pattern 2: path first
    for m in re.finditer(_BUNDLE_PATTERNS[1], text):
        real_path, hash_path, hash_base = m.group(1), m.group(2), m.group(3)
        real_base = os.path.basename(real_path)
        if real_base and not is_hashed_name(os.path.splitext(real_base)[0]):
            rename_map[hash_base] = real_base
    # Pattern 3: src/dest
    for m in re.finditer(_BUNDLE_PATTERNS[2], text):
        hash_path, hash_base, real_path = m.group(1), m.group(2), m.group(3)
        real_base = os.path.basename(real_path)
        if real_base and not is_hashed_name(os.path.splitext(real_base)[0]):
            rename_map[hash_base] = real_base


# ─────────────────────────────────────────────────────────────────────────────
# Основная логика
# ─────────────────────────────────────────────────────────────────────────────


def get_real_name(p: Path, image_rename_map: dict, bundle_map: dict | None = None) -> str | None:
    """Определяет оригинальное имя для файла p. None если не можем."""
    stem = p.stem
    if not is_hashed_name(stem):
        return None

    # ── СТРАТЕГИЯ 0: bundle map — самый надёжный источник ────────────────────
    if bundle_map and p.name in bundle_map:
        real = bundle_map[p.name]
        if real and real != p.name:
            return real

    declared_ext = p.suffix.lower()
    data_head = p.read_bytes()[:1024]

    # Определяем реальное расширение (может отличаться от declared)
    real_ext = _detect_real_ext(data_head, declared_ext)

    # ── Atlas ─────────────────────────────────────────────────────────────────
    if declared_ext in ('.atlas', '.atlas.txt'):
        st = read_atlas_name(p)
        if st:
            return f'{st}{declared_ext}'
        return None

    # ── XML TextureAtlas ───────────────────────────────────────────────────────
    if declared_ext == '.xml' or real_ext == '.xml':
        st = read_xml_atlas_name(p)
        if st:
            return f'{st}{declared_ext}'
        return None

    # ── Изображения ───────────────────────────────────────────────────────────
    if declared_ext in IMAGE_EXTS or real_ext in IMAGE_EXTS:
        if p.name in image_rename_map:
            candidate = image_rename_map[p.name]
            if real_ext != declared_ext:
                candidate = os.path.splitext(candidate)[0] + real_ext
            return candidate
        return None

    # ── JSON / SKEL ──────────────────────────────────────────────────────────
    if declared_ext in ('.json', '.skel') or real_ext == '.json':
        data_bytes = p.read_bytes()
        st = None
        if is_spine_binary(data_bytes):
            st = infer_name_from_binary(data_bytes)
        if not st:
            st = read_json_name(p, data_bytes)
        if not st:
            st = infer_name_from_binary(data_bytes)
        if st and not is_hashed_name(st) and not _looks_like_garbage(st):
            return f'{st}{declared_ext}'
        return None

    # ── Медиа: MP3 / WAV / OGG / SVG ─────────────────────────────────────────
    if declared_ext in ('.mp3', '.wav', '.ogg', '.svg') or real_ext in ('.mp3',):
        for sibling in p.parent.glob('*.json'):
            try:
                raw = sibling.read_text(encoding='utf-8', errors='ignore')
                if p.name in raw:
                    m = re.search(r'"([^"]{3,60})"\s*:\s*"[^"]*' + re.escape(p.name), raw)
                    if m:
                        candidate = strip_short_hash(m.group(1))
                        if candidate and not is_hashed_name(candidate):
                            return f'{candidate}{declared_ext}'
            except Exception:
                pass
        return None

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────────────────────────────────────

def process_folder(src_path: Path, dry_run: bool = False, recursive: bool = True) -> int:
    """Переименовывает файлы с hash-именами в папке и возвращает число изменений."""
    print(f"📁 Обработка: {src_path}")

    bundle_map = build_bundle_rename_map(src_path)
    if bundle_map:
        print(f"   Bundle map: {len(bundle_map)} записей")

    image_rename_map = build_image_rename_map(src_path)
    if image_rename_map:
        print(f"   Atlas map: {len(image_rename_map)} записей")

    count = 0
    skipped = 0
    errors = 0

    # Имена уже занятые в ЭТОМ запуске (чтобы не давать двум файлам одно имя)
    claimed: set = set()

    glob_iter = src_path.rglob('*') if recursive else src_path.glob('*')

    for p in sorted(glob_iter):
        if not p.is_file():
            continue
        if not is_hashed_name(p.stem):
            continue

        try:
            desired = get_real_name(p, image_rename_map, bundle_map)
        except Exception as e:
            print(f"  ❌ ОШИБКА {p.name}: {e}")
            errors += 1
            continue

        if not desired or desired == p.name:
            continue

        # Разрешаем конфликты с диском И с уже заявленными именами этого запуска
        stem_d, ext_d = os.path.splitext(desired)
        final_name = desired
        if (p.parent / final_name).exists() or final_name in claimed:
            # Ищем свободный суффикс
            final_name = None
            for i in range(2, 50):
                candidate = f"{stem_d}_{i}{ext_d}"
                if not (p.parent / candidate).exists() and candidate not in claimed:
                    final_name = candidate
                    print(f"  ⚠️  конфликт → {candidate}")
                    break
        if not final_name:
            print(f"  ⚠️  пропуск (все имена заняты): {p.name[:40]}")
            skipped += 1
            continue

        claimed.add(final_name)

        if dry_run:
            # Показываем нужна ли конвертация
            real_ext = _detect_real_ext(p.read_bytes()[:100], p.suffix.lower())
            needs_conv = real_ext in _NON_SPINE_IMAGE_EXTS and os.path.splitext(final_name)[1].lower() == '.png'
            conv_mark = ' [convert WEBP→PNG]' if needs_conv else ''
            print(f"  [dry-run] {p.name[:40]}... → {final_name}{conv_mark}")
        else:
            # Проверяем: WEBP/AVIF притворяются PNG?
            data_head = p.read_bytes()[:100]
            real_ext = _detect_real_ext(data_head, p.suffix.lower())
            final_ext = os.path.splitext(final_name)[1].lower()

            if real_ext in _NON_SPINE_IMAGE_EXTS and final_ext == '.png':
                # Конвертируем в PNG вместо простого rename
                dst = p.parent / final_name
                if convert_to_png(p, dst):
                    p.unlink()  # Удаляем исходник
                    print(f"  ✅ {p.name[:40]}... → {final_name}  [КОНВЕРТИРОВАНО {real_ext.upper()[1:]}→PNG]")
                else:
                    # Fallback: просто переименовываем если PIL не справился
                    p.rename(dst)
                    print(f"  ✅ {p.name[:40]}... → {final_name}  (rename only, PIL unavail)")
            elif real_ext in _NON_SPINE_IMAGE_EXTS and final_ext != '.png':
                # WEBP/AVIF остаётся non-PNG — всё равно конвертируем в PNG
                png_name = os.path.splitext(final_name)[0] + '.png'
                # Проверяем нет ли конфликта с PNG именем
                if not (p.parent / png_name).exists() and png_name not in claimed:
                    dst = p.parent / png_name
                    claimed.discard(final_name)
                    claimed.add(png_name)
                    if convert_to_png(p, dst):
                        p.unlink()
                        print(f"  ✅ {p.name[:40]}... → {png_name}  [КОНВЕРТИРОВАНО {real_ext.upper()[1:]}→PNG]")
                    else:
                        p.rename(p.parent / final_name)
                        print(f"  ✅ {p.name[:40]}... → {final_name}  (rename only)")
                else:
                    p.rename(p.parent / final_name)
                    print(f"  ✅ {p.name[:40]}... → {final_name}")
            else:
                p.rename(p.parent / final_name)
                print(f"  ✅ {p.name[:40]}... → {final_name}")
        count += 1

    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Переименовано: {count}, пропущено: {skipped}, ошибок: {errors}")

    # ── POST-PROCESS: объединяем последовательные XML файлы ──────────────
    if not dry_run and count > 0:
        merged = merge_xml_sequences(src_path, recursive=recursive)
        if merged:
            print(f"📎 Объединено XML-последовательностей: {merged}")

    return count


# ─────────────────────────────────────────────────────────────────────────────
# XML Merge: объединяет name.xml + name_2.xml + name_3.xml → name.xml
# ─────────────────────────────────────────────────────────────────────────────

def merge_xml_sequences(root_path: Path, recursive: bool = True) -> int:
    """
    Ищет последовательности XML файлов вида:
        name.xml, name_2.xml, name_3.xml, ...
    в одной папке. Если все файлы имеют одинаковый корневой тег (<config>)
    и содержат только дочерние элементы (<string>), объединяет их в один name.xml.

    Возвращает количество объединённых групп.
    """
    import xml.etree.ElementTree as ET

    dirs_to_scan = set()
    if recursive:
        for p in root_path.rglob('*.xml'):
            dirs_to_scan.add(p.parent)
    else:
        dirs_to_scan.add(root_path)

    total_merged = 0

    for folder in sorted(dirs_to_scan):
        # Находим все .xml файлы в этой папке
        xml_files = sorted(folder.glob('*.xml'))
        if len(xml_files) < 2:
            continue

        # Группируем по базовому имени: name.xml → base="name"
        # name_2.xml, name_3.xml → base="name"
        _SEQ_RE = re.compile(r'^(.+?)_(\d+)$')
        groups: dict[str, list[Path]] = {}

        for xf in xml_files:
            stem = xf.stem
            m = _SEQ_RE.match(stem)
            if m:
                base = m.group(1)
            else:
                base = stem
            groups.setdefault(base, []).append(xf)

        for base, files in groups.items():
            if len(files) < 2:
                continue

            # Должен быть хотя бы base.xml И base_N.xml
            base_file = folder / f"{base}.xml"
            numbered = [f for f in files if f != base_file]
            if not numbered:
                continue
            if not base_file.exists():
                # Нет основного файла — берём _2 как основу
                numbered.sort(key=lambda f: int(_SEQ_RE.match(f.stem).group(2)) if _SEQ_RE.match(f.stem) else 0)
                base_file = numbered.pop(0)
                if not numbered:
                    continue

            # Парсим все файлы — проверяем совместимость
            try:
                base_tree = ET.parse(str(base_file))
                base_root = base_tree.getroot()
                root_tag = base_root.tag  # обычно "config"

                # Проверяем: все дочерние должны быть <string> (или аналогичный тег)
                child_tags = set(child.tag for child in base_root)
                if not child_tags:
                    continue
                # Validate that children are the expected <string> element type
                # so incompatible XML structures are not merged.
                if not child_tags.issubset({"string"}):
                    continue
            except Exception:
                continue

            all_ok = True
            parts_to_merge = []

            for nf in sorted(numbered, key=lambda f: int(_SEQ_RE.match(f.stem).group(2)) if _SEQ_RE.match(f.stem) else 99):
                try:
                    tree = ET.parse(str(nf))
                    nr = tree.getroot()
                    if nr.tag != root_tag:
                        all_ok = False
                        break
                    parts_to_merge.append((nf, nr))
                except Exception:
                    all_ok = False
                    break

            if not all_ok or not parts_to_merge:
                continue

            # Проверяем нет ли дубликатов id
            seen_ids = set()
            for child in base_root:
                cid = child.get('id', '')
                if cid:
                    seen_ids.add(cid)

            has_conflict = False
            for nf, nr in parts_to_merge:
                for child in nr:
                    cid = child.get('id', '')
                    if cid and cid in seen_ids:
                        # Дубликат — пропускаем этот элемент но продолжаем
                        pass
                    elif cid:
                        seen_ids.add(cid)

            # Мержим: добавляем все дочерние элементы в base
            added = 0
            existing_ids = set(child.get('id', '') for child in base_root)
            for nf, nr in parts_to_merge:
                for child in nr:
                    cid = child.get('id', '')
                    if cid and cid in existing_ids:
                        continue  # пропускаем дубликат
                    base_root.append(child)
                    existing_ids.add(cid)
                    added += 1

            if added == 0:
                continue

            # Сохраняем объединённый файл
            try:
                # Пишем с xml declaration если был в оригинале
                base_tree.write(str(base_file), encoding='unicode', xml_declaration=False)
                # Оборачиваем в declaration
                content = base_file.read_text(encoding='utf-8')
                if not content.startswith('<?xml'):
                    content = '<?xml version="1.0" encoding="utf-8"?>\n' + content
                base_file.write_text(content, encoding='utf-8')

                # Удаляем пронумерованные файлы
                for nf, _ in parts_to_merge:
                    try:
                        nf.unlink()
                        print(f"  📎 {nf.name} → merged into {base_file.name}")
                    except Exception:
                        pass

                print(f"  ✅ Merged {len(parts_to_merge)} files into {base_file.name} (+{added} elements)")
                total_merged += 1
            except Exception as e:
                print(f"  ❌ Merge error for {base}: {e}")

    return total_merged


def main():
    """Запускает CLI восстановления имён для файла или папки."""
    parser = argparse.ArgumentParser(description='Restore original filenames from hashed names')
    parser.add_argument('path', help='Папка или файл для обработки')
    parser.add_argument('--dry-run', action='store_true', help='Только показать, не переименовывать')
    parser.add_argument('--no-recursive', action='store_true', help='Не обходить вложенные папки')
    args = parser.parse_args()

    src = Path(args.path)
    if not src.exists():
        print(f"ОШИБКА: не найдено: {src}")
        sys.exit(1)

    if src.is_file():
        image_rename_map = build_image_rename_map(src.parent)
        bundle_map = build_bundle_rename_map(src.parent)
        desired = get_real_name(src, image_rename_map, bundle_map)
        if desired:
            final = resolve_conflict(src, desired)
            if final:
                print(f"{src.name} → {final}")
                if not args.dry_run:
                    src.rename(src.parent / final)
            else:
                print(f"Пропуск (дубль): {src.name}")
        else:
            print(f"Не удалось определить оригинальное имя: {src.name}")
    else:
        process_folder(src, dry_run=args.dry_run, recursive=not args.no_recursive)


if __name__ == '__main__':
    main()
