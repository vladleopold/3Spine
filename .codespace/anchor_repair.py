#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Восстановление по якорям.
#
# Шаг 1 (length-preserving): каждый EF BF BD -> 00 00 00, затем
#         3F 00 00 00 -> 3F 80 00 00 (float32 1.0). Длина файла не меняется.
# Шаг 2 (якорный): имена костей в 3.8 идут подряд, и запись кости имеет
#         известный размер, поэтому по имени «root» можно откалибровать размер
#         записи, посчитать кости и починить счётчик секции.
# Шаг 3: тем же способом чинятся счётчики слотов и скинов.
#
# Использование:
#   python3 anchor_repair.py reels_fx.skel -o out/ [--anchor root] [--rec 38]
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if os.path.join(HERE, "spine_restore") not in sys.path:
    sys.path.insert(0, os.path.join(HERE, "spine_restore"))

FFFD = b"\xef\xbf\xbd"


def length_preserving(data: bytes) -> bytes:
    """EF BF BD -> 00 00 00, затем 3F 00 00 00 -> 3F 80 00 00."""
    buf = bytearray()
    i = 0
    while i < len(data):
        if data.startswith(FFFD, i):
            buf += b"\x00\x00\x00"
            i += 3
        else:
            buf.append(data[i])
            i += 1
    j = 0
    while j + 4 <= len(buf):
        if bytes(buf[j:j + 4]) == b"\x3f\x00\x00\x00":
            buf[j + 1:j + 4] = b"\x80\x00\x00"
            j += 4
        else:
            j += 1
    return bytes(buf)


def printable_name(buf: bytes, i: int):
    """Читает имя по правилам 3.8 (varint длина; 0 — null; 1 — ''). Возвращает (имя, след. позиция)."""
    if i >= len(buf):
        return None, i
    n = buf[i]
    if n == 0 or n > 64 or i + n > len(buf):
        return None, i
    raw = buf[i + 1:i + n]
    if len(raw) != n - 1 or not all(33 <= c < 127 for c in raw):
        return None, i
    return raw.decode("ascii"), i + n


def calibrate(buf: bytes, anchor: bytes, recs=range(29, 49)):
    """Подбирает размер записи кости: тот, что даёт самую длинную цепочку имён."""
    best = None
    pos = buf.find(bytes([len(anchor) + 1]) + anchor)   # в 3.8 байт длины = длина + 1
    if pos < 0:
        return None
    for rec in recs:
        names = []
        i = pos
        while len(names) < 400:
            nm, nxt = printable_name(buf, i)
            if nm is None:
                break
            names.append(nm)
            i = nxt + rec
        if best is None or len(names) > len(best[1]):
            best = (rec, names, pos)
    return best


def section_count_offset(buf: bytes, first_name_pos: int, rec: int, n_names: int):
    """Ищет байт со счётчиком перед первой костью: идём назад по записям."""
    # первая кость начинается в first_name_pos; её запись заканчивается перед ней
    pos = first_name_pos
    for _ in range(n_names + 4):
        if pos <= 0:
            break
        prev_end = pos - rec
        nm, _ = printable_name(buf, prev_end)
        if nm is None:
            break
        pos = prev_end
    return pos - 1 if pos > 0 else -1


def repair(path: str, anchor: str = "root", target_bones: int = 0, out_dir: str = None):
    with open(path, "rb") as f:
        data = f.read()
    buf = bytearray(length_preserving(data))
    info = {"file": os.path.basename(path), "bytes": len(buf)}
    cal = calibrate(bytes(buf), anchor.encode())
    if not cal:
        info["error"] = f"якорь '{anchor}' не найден"
        return None, info
    rec, names, pos = cal
    info.update({"record_size": rec, "anchor_pos": hex(pos), "anchor_chain": names[:8],
                 "anchor_count": len(names)})
    off = section_count_offset(bytes(buf), pos, rec, len(names))
    info["count_offset"] = hex(off) if off >= 0 else None
    if off >= 0:
        info["count_value"] = buf[off]
        if target_bones or len(names):
            buf[off] = len(names) & 0x7F if len(names) < 0x80 else 0x7F
    # проверяем разбором
    sys.path.insert(0, HERE)
    import spine38_binary_to_json as P
    r = P.Spine38BinaryReader(bytes(buf))
    try:
        doc = r.read_skeleton_data()
        info["parsed"] = True
        info["bones"] = len(doc.get("bones") or [])
        info["slots"] = len(doc.get("slots") or [])
        info["animations"] = list(doc.get("animations") or {})[:8]
        info["doc"] = doc
    except Exception as e:                          # noqa: BLE001
        info["parsed"] = False
        info["error"] = f"{type(e).__name__}: {e}"
        info["pos"] = getattr(r, "pos", None)
    if out_dir and info.get("parsed"):
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        with open(os.path.join(out_dir, stem + ".anchored.skel"), "wb") as f:
            f.write(bytes(buf))
        with open(os.path.join(out_dir, stem + ".anchored.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        info["saved"] = out_dir
    return bytes(buf), info


def main() -> int:
    ap = argparse.ArgumentParser(description="Восстановление по якорям")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--anchor", default="root")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()
    for path in args.files:
        _buf, info = repair(path, args.anchor, out_dir=args.out)
        info.pop("doc", None)
        print(json.dumps(info, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
