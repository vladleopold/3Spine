"""Извлечение ассетов, вшитых в манифесты движков.

Многие игры (Pragmatic/gs2c, Unity-сборки, разные оболочки) не отдают
Spine-файлы по сети, а кладут их прямо в JSON-манифесты в base64:
    {"type": "UHTSpine", "id": "...", "data": {"name": "logo_SkeletonData",
     "spineJSON": "ewoic2tlbGV0b24p...", "atlas": "...", "textures": {...}}}
Этот модуль находит такие полезные нагрузки без знания конкретного движка.
"""
import base64
import binascii
import json
import re

B64_RE = re.compile(r"^[A-Za-z0-9+/\s=_-]{120,}$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SPINE38_RE = re.compile(rb"^.[0-9a-f]{6,64}[\x00-\x1f]?\d\.\d\.\d{2}")


def is_spine_json(b: bytes) -> bool:
    head = b[:65536]
    if any(k in head for k in (b'"bones"', b'"slots"', b'"animations"', b'"skins"', b'"skeleton"')):
        return True
    return bool(SPINE38_RE.match(head)) or (
        bool(re.search(rb"\d\.\d\.\d{2}", head[:256])) and b'"' not in head[:64])


def is_atlas(b: bytes) -> bool:
    try:
        t = b[:4096].decode("utf-8", "ignore")
    except Exception:                                     # noqa: BLE001
        return False
    if t.lstrip().startswith(("{", "[")):
        return False
    head = t.split("\n\n")[0]
    return ("size:" in head and "\n" in head) or head.strip().endswith(".png")


def clean_name(raw: str) -> str:
    n = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw or "").strip("_")
    n = re.sub(r"(?i)_?skeletondata$", "", n)
    return n or "asset"


def _b64(val: str):
    v = val.strip().replace("-", "+").replace("_", "/")
    if not B64_RE.match(v):
        return None
    try:
        return base64.b64decode(v + "=" * (-len(v) % 4), validate=False)
    except (binascii.Error, ValueError):
        return None


def walk(node, out: dict, hints: tuple = ()):
    """Рекурсивно ищем dict-узлы с именем и base64-полями."""
    if isinstance(node, dict):
        name = ""
        for key in ("name", "id", "key", "fileName", "file", "path", "url"):
            if isinstance(node.get(key), str) and node[key]:
                name = node[key]
                break
        for k, v in node.items():
            if isinstance(v, str) and len(v) > 120 and B64_RE.match(v.strip()):
                data = _b64(v)
                if not data or len(data) < 64:
                    continue
                if data.startswith(PNG_MAGIC):
                    out.setdefault(clean_name(name) + ".png", data)
                elif is_spine_json(data):
                    out.setdefault(clean_name(name) + ".json", data)
                elif is_atlas(data):
                    out.setdefault(clean_name(name) + ".atlas", data)
            elif isinstance(v, (dict, list)):
                walk(v, out, hints)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                walk(item, out, hints)


def loads_lenient(text: str):
    """Многие движки режут манифест на куски: склеиваем фрагменты и разбираем."""
    try:
        return json.loads(text)
    except Exception:                                     # noqa: BLE001
        pass
    dec = json.JSONDecoder()
    objs, idx, n = [], 0, len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n,]}":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = dec.raw_decode(text, idx)
        except ValueError:
            nxt = text.find("{", idx + 1)
            if nxt < 0:
                break
            idx = nxt
            continue
        objs.append(obj)
        idx = end
    return objs or None


OBJ_B64_RE = re.compile(
    r'"(?:name|id|key)"\s*:\s*"([A-Za-z0-9_.\- ]{2,60})"[^{}]{0,400}?'
    r'"([A-Za-z0-9_]{2,30})"\s*:\s*"([A-Za-z0-9+/=\s]{200,})"', re.S)


def extract_text(text: str) -> dict:
    """Фоллбэк для невалидного/резаного JSON: ищем объекты с base64-полями."""
    out: dict = {}
    for m in OBJ_B64_RE.finditer(text):
        name, _field, blob = m.group(1), m.group(2), m.group(3)
        data = _b64(blob)
        if not data or len(data) < 64:
            continue
        if data.startswith(PNG_MAGIC):
            out.setdefault(clean_name(name) + ".png", data)
        elif is_spine_json(data):
            out.setdefault(clean_name(name) + ".json", data)
        elif is_atlas(data):
            out.setdefault(clean_name(name) + ".atlas", data)
    return out


def extract(text_or_obj) -> dict:
    """Возвращает {имя файла: байты} для всех вшитых ассетов."""
    out: dict = {}
    if isinstance(text_or_obj, (bytes, bytearray)):
        try:
            text_or_obj = text_or_obj.decode("utf-8", "replace")
        except Exception:                                 # noqa: BLE001
            return out
    if isinstance(text_or_obj, str):
        obj = loads_lenient(text_or_obj)
        if obj is None:
            got = extract_text(text_or_obj)
            fix_atlas_pages(got)
            return got
    else:
        obj = text_or_obj
    try:
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    walk(item, out)
        else:
            walk(obj, out)
    except RecursionError:
        pass
    fix_atlas_pages(out)
    return out


def _ext_of(data: bytes) -> str:
    if data.startswith(PNG_MAGIC):
        return ".png"
    return ".json" if is_spine_json(data) else ".atlas"


def fix_atlas_pages(out: dict) -> None:
    """Имена страниц в атласе приводим к реально сохранённым файлам."""
    pages = {}
    for name in out:
        stem, ext = os.path.splitext(name)
        if ext.lower() in (".png", ".webp", ".jpg", ".jpeg"):
            pages.setdefault(stem.lower(), name)
    for name in list(out):
        if not name.lower().endswith(".atlas"):
            continue
        try:
            text = out[name].decode("utf-8")
        except Exception:                                 # noqa: BLE001
            continue
        lines = []
        for line in text.split("\n"):
            parts = line.rstrip("\r").split(":")
            if len(parts) >= 3 and not line.startswith((" ", "\t")) and \
                    re.search(r"\.(png|webp|jpg|jpeg)$", parts[0].strip(), re.I):
                page = parts[0].strip()
                stem = re.sub(r"\.(png|webp|jpg|jpeg)$", "", page, flags=re.I).lower()
                real = pages.get(stem) or pages.get(re.sub(r"[^a-z0-9]", "", stem))
                if real:
                    parts[0] = real
                    line = ":".join(parts)
            lines.append(line)
        out[name] = "\n".join(lines).encode("utf-8")


import os  # noqa: E402  (используется в fix_atlas_pages)
