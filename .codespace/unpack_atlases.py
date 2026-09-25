#!/usr/bin/env python3
"""Unpack atlas files (Spine .atlas and common JSON atlas formats) into separate images.

Usage: python3 tools/unpack_atlases.py /path/to/atlas_dir --output /path/to/output_dir
"""
from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image

IMAGE_EXTS = ('.png', '.webp', '.jpg', '.jpeg', '.bmp', '.avif', '.tiff', '.tif', '.gif')

# Valueless page properties (no ':' separator) that may appear between the
# page image line and the first region. Region names are bare lines too, so
# we only consume these known keywords — anything else is treated as a region.
_VALUELESS_PAGE_PROPS = frozenset(('repeat', 'mipmaps', 'pma'))


def _is_hashed(text: str) -> bool:
    if not text:
        return False
    base = os.path.splitext(text)[0] if '.' in text else text
    return len(base) >= 32 and re.fullmatch(r'[0-9a-fA-F]+', base) is not None


def _read_atlas_first_line(atlas_path: str) -> Optional[str]:
    try:
        with open(atlas_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line:
                    return line
    except Exception:
        pass
    return None


def _resolve_texture_name(tex_name: str, atlas_path: Optional[str] = None) -> str:
    if not _is_hashed(tex_name):
        return tex_name
    if atlas_path and _is_hashed(os.path.basename(atlas_path)):
        real = _read_atlas_first_line(atlas_path)
        if real:
            return real
    return tex_name


def parse_spine_atlas(path: str) -> List[Dict[str, Any]]:
    with open(path, 'r', encoding='utf-8') as f:
        lines = [l.rstrip('\n') for l in f]

    pages = []
    i = 0
    n = len(lines)
    while i < n:
        # skip blank lines
        while i < n and lines[i].strip() == '':
            i += 1
        if i >= n:
            break
        # page image line (must look like a filename with an image extension)
        page_image = lines[i].strip()
        _, _pext = os.path.splitext(page_image)
        if _pext.lower() not in IMAGE_EXTS:
            # Not a page image — skip non-conforming line (stray property / malformed atlas)
            i += 1
            continue
        page_image = _resolve_texture_name(page_image, path)
        i += 1
        # read page properties while line looks like a key: value property
        # or a valueless page property (e.g. 'repeat'); stop at the next page image
        while i < n:
            line = lines[i].strip()
            if line == '':
                break
            if ':' in line:
                i += 1
                continue
            _, _pext2 = os.path.splitext(line)
            if _pext2.lower() in IMAGE_EXTS:
                break  # next page
            if line.lower() in _VALUELESS_PAGE_PROPS:
                i += 1
                continue  # valueless page property (e.g. 'repeat')
            break  # bare line -> first region name

        regions = []
        # read region blocks until next page (identified by a line that looks like a filename)
        while True:
            # skip blanks
            while i < n and lines[i].strip() == '':
                i += 1
            if i >= n:
                break
            candidate = lines[i].strip()
            # if candidate looks like a page image filename, treat as next page
            _, ext = os.path.splitext(candidate)
            if ext.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.avif', '.tiff', '.tif', '.gif'):
                break
            # otherwise candidate is a region name
            name = candidate
            if not name:
                i += 1
                continue
            i += 1
            attrs = {}
            while i < n:
                line = lines[i].strip()
                if line == '':
                    i += 1
                    break
                if ':' not in line:
                    break
                k, v = [s.strip() for s in line.split(':', 1)]
                attrs[k] = v
                i += 1
            # parse attrs we care about
            rotate = attrs.get('rotate', 'false').lower() not in ('false', '0', '')
            bounds = attrs.get('bounds')
            xy = attrs.get('xy', '0,0')
            size = attrs.get('size', '0,0')
            offsets = attrs.get('offsets')
            orig = attrs.get('orig')
            offset = attrs.get('offset')
            try:
                if bounds:
                    x, y, w, h = [int(s.strip()) for s in bounds.split(',')]
                else:
                    x, y = [int(s.strip()) for s in xy.split(',')]
                    w, h = [int(s.strip()) for s in size.split(',')]
                ox = oy = ow = oh = 0
                if offsets:
                    off_parts = [s.strip() for s in offsets.split(',')]
                    if len(off_parts) == 4:
                        ox, oy, ow, oh = [int(s) for s in off_parts]
                elif orig and offset:
                    try:
                        ow_s, oh_s = [s.strip() for s in orig.split(',')]
                        ow, oh = int(ow_s), int(oh_s)
                        ox_s, oy_s = [s.strip() for s in offset.split(',')]
                        ox, oy = int(ox_s), int(oy_s)
                    except Exception:
                        ox = oy = ow = oh = 0
                elif orig:
                    try:
                        ow_s, oh_s = [s.strip() for s in orig.split(',')]
                        ow, oh = int(ow_s), int(oh_s)
                    except Exception:
                        pass
            except Exception:
                x = y = w = h = 0
            regions.append({'name': name, 'x': x, 'y': y, 'w': w, 'h': h, 'rotate': rotate, 'offsets': {'x': ox, 'y': oy, 'w': ow, 'h': oh}, 'orig': {'w': ow, 'h': oh}, 'offset': {'x': ox, 'y': oy}})

        pages.append({'image': page_image, 'regions': regions})
    return pages


def parse_json_atlas(path: str) -> List[Dict[str, Any]]:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    pages = []
    base_image = None
    if isinstance(data, dict):
        # TexturePacker style
        frames = data.get('frames')
        meta = data.get('meta', {})
        if isinstance(meta, dict):
            base_image = meta.get('image')
        if isinstance(frames, dict):
            regions = []
            for name, info in frames.items():
                # info might be a dict with 'frame' or with x/y/w/h directly
                fr = info.get('frame') if isinstance(info, dict) and 'frame' in info else info
                if fr and isinstance(fr, dict):
                    x = int(fr.get('x', 0) or 0)
                    y = int(fr.get('y', 0) or 0)
                    w = int(fr.get('w', fr.get('width', 0)) or 0)
                    h = int(fr.get('h', fr.get('height', 0)) or 0)
                    rotated = False
                    if isinstance(info, dict):
                        rotated = bool(info.get('rotated', False))
                    sprite_source = None
                    source_size = None
                    if isinstance(info, dict):
                        ss = info.get('spriteSourceSize')
                        if isinstance(ss, dict):
                            sprite_source = {
                                'x': int(ss.get('x', 0) or 0),
                                'y': int(ss.get('y', 0) or 0),
                                'w': int(ss.get('w', 0) or 0),
                                'h': int(ss.get('h', 0) or 0),
                            }
                        sz = info.get('sourceSize')
                        if isinstance(sz, dict):
                            source_size = {
                                'w': int(sz.get('w', 0) or 0),
                                'h': int(sz.get('h', 0) or 0),
                            }
                    regions.append({
                        'name': name,
                        'x': x,
                        'y': y,
                        'w': w,
                        'h': h,
                        'rotate': rotated,
                        'sprite_source_size': sprite_source,
                        'source_size': source_size,
                    })
            pages.append({'image': base_image or '', 'regions': regions})
        elif isinstance(frames, list):
            # TexturePacker frames-as-list format: each entry is a region.
            regions = []
            for entry in frames:
                if not isinstance(entry, dict):
                    continue
                fr = entry.get('frame') if 'frame' in entry else entry
                if not isinstance(fr, dict):
                    continue
                name = entry.get('filename') or entry.get('name') or ''
                if not name:
                    continue
                x = int(fr.get('x', 0) or 0)
                y = int(fr.get('y', 0) or 0)
                w = int(fr.get('w', fr.get('width', 0)) or 0)
                h = int(fr.get('h', fr.get('height', 0)) or 0)
                rotated = bool(entry.get('rotated', False))
                sprite_source = None
                source_size = None
                ss = entry.get('spriteSourceSize')
                if isinstance(ss, dict):
                    sprite_source = {
                        'x': int(ss.get('x', 0) or 0),
                        'y': int(ss.get('y', 0) or 0),
                        'w': int(ss.get('w', 0) or 0),
                        'h': int(ss.get('h', 0) or 0),
                    }
                sz = entry.get('sourceSize')
                if isinstance(sz, dict):
                    source_size = {
                        'w': int(sz.get('w', 0) or 0),
                        'h': int(sz.get('h', 0) or 0),
                    }
                regions.append({
                    'name': name,
                    'x': x,
                    'y': y,
                    'w': w,
                    'h': h,
                    'rotate': rotated,
                    'sprite_source_size': sprite_source,
                    'source_size': source_size,
                })
            pages.append({'image': base_image or '', 'regions': regions})
    return pages


def choose_strategy(atlas_path: str, pages: List[Dict[str, Any]]) -> str:
    try:
        if atlas_path.lower().endswith('.json'):
            has_rotated = False
            for page in pages:
                for r in page.get('regions', []):
                    if r.get('rotate'):
                        has_rotated = True
                        break
                if has_rotated:
                    break
            return 'tp_json_rotated' if has_rotated else 'tp_json'
        else:
            has_bounds = False
            has_offsets = False
            for page in pages:
                for r in page.get('regions', []):
                    off = r.get('offsets') if isinstance(r, dict) else None
                    if isinstance(off, dict) and off.get('w') and off.get('h'):
                        has_offsets = True
            try:
                with open(atlas_path, 'r', encoding='utf-8') as f:
                    text = f.read()
                if 'bounds' in text:
                    has_bounds = True
            except Exception:
                pass
            if has_bounds and has_offsets:
                return 'spine_bounds_offsets_rotated'
            if has_bounds:
                return 'spine_bounds'
            return 'spine_xy'
    except Exception:
        return 'auto'


def extract_regions(atlas_path: str, pages: List[Dict[str, Any]], atlas_dir: str, out_dir: str, dest_name: Optional[str] = None, trim: Optional[Dict[str, Any]] = None, page_images: Optional[List[str]] = None, rotate_mode: str = "90", image_renames: Optional[Dict[str, List[str]]] = None, extra_image_dirs: Optional[List[str]] = None) -> None:
    strategy = choose_strategy(atlas_path, pages)
    atlas_name = os.path.splitext(os.path.basename(atlas_path))[0]
    if dest_name == "" or os.path.basename(out_dir).lower() == "images":
        dest_base = out_dir
    else:
        dest_base = os.path.join(out_dir, dest_name or atlas_name)
    written = 0
    trim = trim or {}
    fallback_trim_horiz = bool(trim.get('horizontal_trim', False))
    fallback_trim_vert = bool(trim.get('vertical_trim', False))

    def _fallback_trim(img, box, x, y, w, h):
        if w <= 0 or h <= 0:
            return x, y, w, h
        x1, y1, x2, y2 = box
        left = top = right = bottom = 0
        if fallback_trim_horiz:
            for col in range(x1, x2):
                if any(img.getpixel((col, row))[3] for row in range(y1, y2)):
                    break
                left += 1
            for col in range(x2 - 1, x1 - 1, -1):
                if any(img.getpixel((col, row))[3] for row in range(y1, y2)):
                    break
                right += 1
        if fallback_trim_vert:
            for row in range(y1, y2):
                if any(img.getpixel((c, row))[3] for c in range(x1, x2)):
                    break
                top += 1
            for row in range(y2 - 1, y1 - 1, -1):
                if any(img.getpixel((c, row))[3] for c in range(x1, x2)):
                    break
                bottom += 1
        if left + right >= w or top + bottom >= h:
            return x, y, w, h
        return x + left, y + top, w - left - right, h - top - bottom

    os.makedirs(dest_base, exist_ok=True)
    # Build a combined list of directories to search for images
    search_dirs = [atlas_dir]
    if extra_image_dirs:
        for d in extra_image_dirs:
            if d and os.path.isdir(d) and d not in search_dirs:
                search_dirs.append(d)

    def _find_image(image_name):
        """Search for image_name across all search_dirs, trying multiple extensions."""
        if not image_name:
            return None
        base, _ext = os.path.splitext(os.path.basename(image_name))
        atlas_basename = os.path.splitext(os.path.basename(atlas_path))[0]
        for search_dir in search_dirs:
            # 1. Try exact name
            candidate = os.path.join(search_dir, image_name)
            if os.path.exists(candidate):
                return candidate
            # 2. Try basename with different extensions
            for ext in ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.avif'):
                candidate = os.path.join(search_dir, base + ext)
                if os.path.exists(candidate):
                    return candidate
            # 3. Try hashed atlas name as image filename
            if _is_hashed(atlas_basename):
                for ext in ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.avif'):
                    candidate = os.path.join(search_dir, atlas_basename + ext)
                    if os.path.exists(candidate):
                        return candidate
            # 4. Try image_renames mapping
            if image_renames:
                for cand in image_renames.get(os.path.basename(image_name), []):
                    candidate = os.path.join(search_dir, cand)
                    if os.path.exists(candidate):
                        return candidate
        return None

    for page in pages:
        image_name = page.get('image') or ''
        if image_name == '':
            # try with atlas basename + .png
            image_name = atlas_name + '.png'
        image_path = _find_image(image_name)
        if not image_path:
            print(f"Warning: page image not found: {os.path.join(atlas_dir, image_name)} (skipping page)")
            continue

        # Открываем изображение; WEBP/AVIF конвертируем в PNG на диске
        # чтобы Spine мог найти файл в следующий раз
        _WEBP_AVIF_SIGS = (b'RIFF', b'\x00\x00\x00')  # RIFF=WEBP, ftyp=AVIF
        _need_convert = False
        try:
            with open(image_path, 'rb') as _f:
                _head = _f.read(12)
            if (_head[:4] == b'RIFF' and _head[8:12] == b'WEBP') or _head[4:8] == b'ftyp':
                _need_convert = True
        except Exception:
            pass

        if _need_convert:
            _png_path = os.path.splitext(image_path)[0] + '.png'
            if not os.path.exists(_png_path):
                try:
                    _tmp = Image.open(image_path).convert('RGBA')
                    _tmp.save(_png_path, 'PNG')
                    _tmp.close()
                    print(f"  Converted {os.path.basename(image_path)} → {os.path.basename(_png_path)}")
                except Exception as _ce:
                    print(f"  Warning: could not convert {os.path.basename(image_path)} to PNG: {_ce}")
            # Используем PNG если он есть
            if os.path.exists(_png_path):
                image_path = _png_path

        img = Image.open(image_path).convert('RGBA')
        try:
            img.load()
        except Exception:
            pass
        for r in page['regions']:
            x, y, w, h = r['x'], r['y'], r['w'], r['h']
            if w <= 0 or h <= 0:
                continue
            # Spine stores rotated regions swapped in atlas
            if r.get('rotate'):
                box = (x, y, x + h, y + w)
            else:
                box = (x, y, x + w, y + h)
            # Validate/clamp bbox against the source image bounds
            iw, ih = img.size
            bx1 = max(0, min(box[0], iw))
            by1 = max(0, min(box[1], ih))
            bx2 = max(0, min(box[2], iw))
            by2 = max(0, min(box[3], ih))
            if bx2 <= bx1 or by2 <= by1:
                continue
            box = (bx1, by1, bx2, by2)
            if not trim.get('disabled', False):
                try:
                    # _fallback_trim expects box and x,y,w,h; handle rotate swap for trim as well
                    if r.get('rotate'):
                        # for fallback, swap w/h for the check
                        x, y, w, h = _fallback_trim(img, box, x, y, h, w)
                        # swap back for subsequent logic (w,h are trimmed size before rotation)
                        w, h = h, w
                    else:
                        x, y, w, h = _fallback_trim(img, box, x, y, w, h)
                except Exception:
                    pass
            if w <= 0 or h <= 0:
                continue
            if r.get('rotate'):
                box = (x, y, x + h, y + w)
            else:
                box = (x, y, x + w, y + h)
            cropped = img.crop(box)
            if r.get('rotate'):
                # Spine rotate true means region is stored 90° CW, need to rotate CCW (90) to restore
                # Use 90 CCW by default; allow rotate_mode to override but normalize
                try:
                    angle = int(rotate_mode)
                    # if angle is -90, convert to 90 CCW for correct restoration
                    if angle == -90:
                        angle = 90
                    elif angle == 90:
                        angle = 90
                    else:
                        angle = 90
                except Exception:
                    angle = 90
                cropped = cropped.rotate(angle, expand=True)
            # preserve directory structure if region name contains '/'
            raw_name = r.get('name', '')
            norm = raw_name.replace('\\', '/').strip('/')
            norm = re.sub(r'^(?:res/img/|res/images/|res/|img/|src/img/|assets/)+', '', norm, flags=re.IGNORECASE)
            parts = [p for p in norm.split('/') if p]
            # sanitize each path part to avoid traversal or absolute paths
            safe_parts = [p.replace('..', '_').replace('/', '_').replace('\\', '_') for p in parts]
            if not safe_parts:
                filename = 'unnamed.png'
                file_dir = dest_base
            else:
                base_name = os.path.splitext(safe_parts[-1])[0]
                filename = (base_name if base_name else safe_parts[-1]) + '.png'
                file_dir = os.path.join(dest_base, *safe_parts[:-1]) if len(safe_parts) > 1 else dest_base
            os.makedirs(file_dir, exist_ok=True)
            out_path = os.path.join(file_dir, filename)
            offsets_info = r.get('offsets') if isinstance(r, dict) else None
            if isinstance(offsets_info, dict) and offsets_info.get('w') and offsets_info.get('h'):
                try:
                    sw = int(offsets_info.get('w', 0) or 0)
                    sh = int(offsets_info.get('h', 0) or 0)
                    sx = int(offsets_info.get('x', 0) or 0)
                    sy = int(offsets_info.get('y', 0) or 0)
                    if sw > 0 and sh > 0:
                        out_img = Image.new('RGBA', (sw, sh), (0, 0, 0, 0))
                        out_img.paste(cropped, (sx, sy))
                        out_img.save(out_path)
                        written += 1
                        continue
                except Exception:
                    pass
            ss = r.get('sprite_source_size') if isinstance(r, dict) else None
            src = r.get('source_size') if isinstance(r, dict) else None
            if ss and isinstance(ss, dict) and src and isinstance(src, dict):
                try:
                    sw = int(src.get('w', 0) or 0)
                    sh = int(src.get('h', 0) or 0)
                    sx = int(ss.get('x', 0) or 0)
                    sy = int(ss.get('y', 0) or 0)
                    if sw > 0 and sh > 0:
                        out_img = Image.new('RGBA', (sw, sh), (0, 0, 0, 0))
                        out_img.paste(cropped, (sx, sy))
                        out_img.save(out_path)
                        written += 1
                        continue
                except Exception:
                    pass
            cropped.save(out_path)
            written += 1

    return written


def _collect_image_dirs(src: str) -> List[str]:
    """Walk src and collect all directories likely containing textures.
    Includes all sibling and parallel folders - critical when atlas and PNG
    are in different subdirectories (e.g. game-main6 atlas, game-main3 PNG).
    """
    found = set()
    src = os.path.abspath(src)
    img_exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.avif')

    def _scan_for_images(root_dir):
        """Add directory to found if it directly contains image files."""
        try:
            for fn in os.listdir(root_dir):
                if fn.lower().endswith(img_exts):
                    found.add(root_dir)
                    return True
        except (PermissionError, OSError):
            pass
        return False

    # 1. Scan src itself recursively
    for root, dirs, fnames in os.walk(src):
        dirs[:] = [d for d in dirs if d not in (
            '.git', '__pycache__', 'output', 'build', 'dist', 'unpacked'
        ) and not d.startswith('output')]
        _scan_for_images(root)

    # 2. Scan sibling directories at the same level as src
    parent = os.path.dirname(src)
    if parent and os.path.isdir(parent):
        for sibling in os.listdir(parent):
            sibling_path = os.path.join(parent, sibling)
            if not os.path.isdir(sibling_path) or sibling_path == src:
                continue
            # Check sibling itself
            _scan_for_images(sibling_path)
            # Check one level deep inside sibling
            try:
                for sub in os.listdir(sibling_path):
                    sub_path = os.path.join(sibling_path, sub)
                    if os.path.isdir(sub_path):
                        _scan_for_images(sub_path)
            except (PermissionError, OSError):
                pass

    # 3. Scan grandparent-level siblings (res/ → game-main1..N)
    grandparent = os.path.dirname(parent) if parent else None
    if grandparent and os.path.isdir(grandparent):
        for item in os.listdir(grandparent):
            item_path = os.path.join(grandparent, item)
            if not os.path.isdir(item_path) or item_path == parent:
                continue
            _scan_for_images(item_path)
            try:
                for sub in os.listdir(item_path):
                    sub_path = os.path.join(item_path, sub)
                    if os.path.isdir(sub_path):
                        _scan_for_images(sub_path)
            except (PermissionError, OSError):
                pass

    return list(found)



def unpack(src: str, output: Optional[str] = None, rotate_mode: str = "90", renames: Optional[Dict[str, List[str]]] = None, extra_image_dirs: Optional[List[str]] = None) -> Tuple[int, Dict[str, str]]:
    src = os.path.abspath(src)
    out = os.path.abspath(output) if output else os.path.join(src, 'unpacked')
    os.makedirs(out, exist_ok=True)

    # Auto-discover image directories to help texture lookup
    auto_dirs = _collect_image_dirs(src)
    all_extra_dirs = list(auto_dirs)
    if extra_image_dirs:
        for d in extra_image_dirs:
            if d and d not in all_extra_dirs:
                all_extra_dirs.append(d)

    files = []
    for root, dirs, fnames in os.walk(src):
        # Don't skip 'images' dirs — atlases may live inside them too
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'output', 'build', 'dist', 'unpacked') and not d.startswith('output')]
        for fn in fnames:
            if fn.lower().endswith(('.atlas', '.atlas.txt', '.json', '.pack', '.plist')):
                files.append(os.path.join(root, fn))

    if not files:
        print('No atlas files found in', src)
        return 0, {}

    workers = int(os.environ.get('UNPACK_WORKERS', '0') or 0)
    if workers <= 0:
        workers = min(8, max(2, (os.cpu_count() or 2)))
    workers = max(1, min(workers, len(files)))

    def _one(f: str) -> Tuple[int, Optional[str]]:
        atlas_dir = os.path.dirname(f)
        pages = []
        try:
            if f.lower().endswith('.json'):
                try:
                    with open(f, 'r', encoding='utf-8') as jf:
                        json.load(jf)
                except Exception:
                    return 0, None
                pages = parse_json_atlas(f)
            else:
                try:
                    pages = parse_json_atlas(f)
                    if not pages:
                        pages = parse_spine_atlas(f)
                except Exception:
                    pages = parse_spine_atlas(f)
        except Exception as e:
            print('Error parsing', f, e)
            return 0, None
        if not pages:
            return 0, None
        strategy = choose_strategy(f, pages)
        dest_name = None
        atlas_basename = os.path.splitext(os.path.basename(f))[0]
        if _is_hashed(atlas_basename) and not f.lower().endswith('.json'):
            real = _read_atlas_first_line(f)
            if real:
                dest_name = os.path.splitext(real)[0] if '.' in real else real
        json_path = os.path.join(atlas_dir, atlas_basename + '.json')
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as jf:
                    data = json.load(jf)
                images_path = data.get('skeleton', {}).get('images', '') if isinstance(data.get('skeleton'), dict) else ''
                if images_path:
                    norm = images_path.replace('\\', '/').strip('/')
                    if norm and not dest_name:
                        dest_name = os.path.basename(norm)
            except Exception:
                pass

        dirs_for_atlas = list(all_extra_dirs)
        if atlas_dir not in dirs_for_atlas:
            dirs_for_atlas.insert(0, atlas_dir)

        try:
            n = extract_regions(
                f, pages, atlas_dir, out,
                dest_name=dest_name,
                page_images=[os.path.join(atlas_dir, p.get('image')) for p in pages if p.get('image')],
                rotate_mode=rotate_mode,
                image_renames=renames,
                extra_image_dirs=dirs_for_atlas,
            )
            return int(n or 0), strategy
        except Exception as e:
            print('Error extracting from', f, e)
            return 0, strategy

    files.sort()
    count = 0
    strategies = {}
    if workers == 1:
        results = [_one(f) for f in files]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_one, files))

    for f, (n, strategy) in zip(files, results):
        count += n
        if strategy:
            strategies[f] = strategy

    print(f'Done. Extracted {count} images with {workers} workers into {out}')
    return count, strategies


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('src', help='Source directory containing atlas files')
    parser.add_argument('--output', '-o', help='Output directory', default=None)
    args = parser.parse_args()
    unpack(args.src, args.output)


if __name__ == '__main__':
    main()
