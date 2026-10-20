#!/usr/bin/env python3
"""Additive raster normalisation used by the conversion pipeline.

The original asset is never removed.  WEBP/AVIF (including files with a
misleading extension) and standalone data:image files get a PNG sibling.
This keeps every existing provider-specific path intact while giving atlas
unpacking and downstream tools a universally supported PNG fallback.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
from io import BytesIO


_DATA_URL = re.compile(
    rb"^\s*data:image/(?:webp|avif|heif|png|jpe?g);(?:charset=[^;,]+;)?base64,\s*",
    re.I,
)


def _kind(head: bytes) -> str:
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (
            b"avif", b"avis", b"heic", b"heix", b"mif1", b"msf1"):
        return "avif"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return ""


def _png_name(path: str, data_url: bool = False) -> str:
    base = path[:-4] if data_url and path.lower().endswith(".txt") else path
    stem, ext = os.path.splitext(base)
    if ext.lower() in (".webp", ".avif", ".heif", ".heic", ".jpg", ".jpeg"):
        return stem + ".png"
    return base if ext.lower() == ".png" else base + ".png"


def _pillow_to_png(source, dst: str) -> bool:
    try:
        try:
            import pillow_avif  # noqa: F401 - registers the AVIF decoder
        except Exception:
            pass
        from PIL import Image
        with Image.open(source) as image:
            image.seek(0)
            image.convert("RGBA").save(dst, "PNG", optimize=True)
        return os.path.isfile(dst) and os.path.getsize(dst) > 64
    except Exception:
        return False


def _external_to_png(path: str, dst: str) -> bool:
    commands = []
    magick = shutil.which("magick")
    convert = shutil.which("convert")
    ffmpeg = shutil.which("ffmpeg")
    avifdec = shutil.which("avifdec")
    if magick:
        commands.append([magick, path + "[0]", dst])
    elif convert:
        commands.append([convert, path + "[0]", dst])
    if ffmpeg:
        commands.append([ffmpeg, "-v", "error", "-y", "-i", path,
                         "-frames:v", "1", dst])
    if avifdec:
        commands.append([avifdec, path, dst])
    for command in commands:
        try:
            subprocess.run(command, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=90, check=True)
            if os.path.isfile(dst) and os.path.getsize(dst) > 64:
                return True
        except Exception:
            pass
    return False


def convert_to_png(path: str) -> str | None:
    """Create a PNG sibling for a raster path and return it.

    Content signatures win over extensions, so ``texture.png`` containing
    WEBP/AVIF is handled too.  Existing real PNG files are returned unchanged.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    kind = _kind(head)
    ext = os.path.splitext(path)[1].lower()
    if kind == "png" and ext == ".png":
        return path
    if not kind and ext not in (".webp", ".avif", ".heif", ".heic"):
        return None
    dst = _png_name(path)
    if os.path.abspath(dst) == os.path.abspath(path):
        fd, tmp = tempfile.mkstemp(prefix="image-normalize-", suffix=".png",
                                   dir=os.path.dirname(path) or None)
        os.close(fd)
        ok = _pillow_to_png(path, tmp) or _external_to_png(path, tmp)
        if ok:
            os.replace(tmp, dst)
            return dst
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    if os.path.isfile(dst) and os.path.getsize(dst) > 64:
        return dst
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    return dst if (_pillow_to_png(path, dst) or _external_to_png(path, dst)) else None


def decode_data_image(path: str) -> str | None:
    """Decode a standalone ``data:image/...;base64`` TXT file to PNG."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    match = _DATA_URL.match(raw)
    if not match:
        return None
    payload = re.sub(rb"\s+", b"", raw[match.end():])
    try:
        payload += b"=" * ((4 - len(payload) % 4) % 4)
        blob = base64.b64decode(payload, validate=False)
    except Exception:
        return None
    if not _kind(blob[:16]):
        return None
    dst = _png_name(path, data_url=True)
    if os.path.isfile(dst) and os.path.getsize(dst) > 64:
        return dst
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    return dst if _pillow_to_png(BytesIO(blob), dst) else None


def normalize_tree(root: str) -> dict:
    """Create PNG fallbacks recursively without changing original paths."""
    report = {"converted": 0, "data_uri": 0, "failed": 0, "files": []}
    candidates = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
        for name in filenames:
            low = name.lower()
            if low.endswith((".webp", ".avif", ".heif", ".heic", ".png", ".txt")):
                candidates.append(os.path.join(dirpath, name))
    for path in candidates:
        if path.lower().endswith(".txt"):
            out = decode_data_image(path)
            if out:
                report["data_uri"] += 1
                report["files"].append(os.path.relpath(out, root))
            continue
        try:
            with open(path, "rb") as fh:
                kind = _kind(fh.read(16))
        except OSError:
            continue
        if kind not in ("webp", "avif") and not path.lower().endswith(
                (".webp", ".avif", ".heif", ".heic")):
            continue
        out = convert_to_png(path)
        if out:
            report["converted"] += 1
            report["files"].append(os.path.relpath(out, root))
        else:
            report["failed"] += 1
    return report
