"""Проверка скачанного: отсекаем soft-404, HTML-ошибки и прочий мусор.

Многие CDN отдают страницу ошибки с кодом 200 — без проверки содержимого
в архив попадают .json/.skel, которые на самом деле HTML.
"""
import json
import re

HTML_MARKERS = (b"<!DOCTYPE html", b"<!doctype html", b"<html", b"<HTML", b"<?xml", b"<head>",
                b"<body", b"Access Denied", b"error code:", b"<title>")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# часть игр отдаёт текстуры в AVIF/JPEG/WebP, хотя расширение остаётся .png
IMAGE_MAGICS = (b"\x89PNG\r\n\x1a\n", b"\x00\x00\x00\x1cftypavif", b"\xff\xd8\xff",
                b"RIFF", b"\x00\x00\x00\x18ftypheic", b"\x00\x00\x00\x1cftypheic")
JSONP_WS = re.compile(rb"^[\s]*[\[{]")
SPINE38_RE = re.compile(rb"^.[0-9a-f]{6,64}[\x00-\x1f]?\d\.\d\.\d{2}")
VERSION_RE = re.compile(rb"\d\.\d+(?:\.\d+)?")


def looks_spine_json(data: bytes) -> bool:
    """Тот же принцип, что у skeleton_router: 4.x — по ключам, 3.8 — по версии,
    перед которой стоит управляющий байт длины хэша (1..32)."""
    head = data[:65536]
    if any(k in head for k in (b'"bones"', b'"slots"', b'"animations"',
                               b'"skins"', b'"skeleton"')):
        return True
    m = VERSION_RE.search(data[:1024])
    if m and m.start() >= 1 and 1 <= data[m.start() - 1] <= 32:
        return True
    return bool(SPINE38_RE.match(data[:256]))


def is_html_or_error(data: bytes) -> bool:
    head = data[:512].lstrip()
    if any(head.lower().startswith(m.lower()) for m in HTML_MARKERS):
        return True
    low = data[:2048].lower()
    return b"<html" in low or b"<!doctype" in low


def classify_response(path: str, data: bytes) -> str:
    """ok | manifest | reject — трехсостоянийная проверка.

    manifest — валидный JSON движка, в нём сам скелетом не является, но из него
    вытаскиваются вшитые ассеты, поэтому его сохраняем отдельно.
    """
    kind, _why = check(path, data)
    if kind:
        return "ok"
    name = path.lower()
    ext = name[name.rfind("."):] if "." in name else ""
    if ext in (".json", ".js", ".txt", ".manifest") and not is_html_or_error(data) and len(data) > 256:
        head = data[:4096].lstrip()
        if head[:1] in (b"{", b"["):
            return "manifest"
    return "reject"


def check(path: str, data: bytes) -> tuple:
    """(ok, причина). Ничего не выбрасываем без явной причины."""
    name = path.lower()
    ext = name[name.rfind("."):] if "." in name else ""
    if len(data) < 32:
        return False, "слишком мал (%d Б)" % len(data)
    if is_html_or_error(data):
        return False, "HTML/страница ошибки"

    if ext == ".json":
        if looks_spine_json(data):
            return True, ""
        head = data[:2048].lstrip()
        if head[:1] in (b"{", b"["):
            try:
                json.loads(data.decode("utf-8", "replace"))
                return True, ""
            except Exception:                             # noqa: BLE001
                return False, "битый JSON"
        return False, "не Spine JSON"

    if ext == ".atlas":
        try:
            t = data[:4096].decode("utf-8", "ignore")
        except Exception:                                 # noqa: BLE001
            return False, "не текст"
        head = t.split("\n\n")[0]
        if ("size:" in head or "filter:" in head or "format:" in head) and \
                re.search(r"\.(png|webp|jpg|jpeg)", t[:2048], re.I):
            return True, ""
        if JSONP_WS.match(data[:4]):
            return False, "это JSON, не атлас"
        return False, "не похож на атлас"

    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        if data.startswith(IMAGE_MAGICS):
            return True, ""
        return False, "не изображение"

    if ext in (".skel", ".bin", ".data"):
        m = VERSION_RE.search(data[:512])
        if m and 1 <= data[m.start() - 1] <= 32 if m.start() >= 1 else False:
            return True, ""
        if ext == ".skel":
            return False, "не Spine binary"
        return True, ""

    if ext in (".webp", ".jpg", ".jpeg", ".ktx", ".ktx2"):
        return True, ""
    return True, ""
