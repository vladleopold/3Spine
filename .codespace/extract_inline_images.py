#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Распаковка картинок, спрятанных внутри JSON/TXT, в настоящие .png.
# Порядок работы логики — как в DataExtractor (main.py): рекурсивно собираем
# строки, начинающиеся с data:image, декодируем base64, отбрасываем мелкие и
# битые. Отличие: понимаем потоковый формат "},{...}" (json.loads на нём падает),
# пишем в каталог артефактов и сохраняем имя записи, а не номер.
#
# Использование: python3 .codespace/extract_inline_images.py <каталог> [--min-kb N] [--out images]
import argparse
import base64
import binascii
import json
import os
import re
import sys
from collections import Counter

UA_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def iter_json_objects(text: str):
    """Отдаёт объекты и из цельного JSON, и из потока },{."""
    t = text.lstrip("\ufeff").strip()
    if not t:
        return
    try:
        yield json.loads(t)
        return
    except json.JSONDecodeError:
        pass
    # поток объектов: "}\r\n{\"...\"},\r\n{\"...\"}" — режем по закрывающей скобке с запятой
    body = t.lstrip("},").strip()
    if body.endswith("}"):
        body = body[:-1]
    for chunk in re.split(r"\}\s*,\s*\{", body):
        chunk = chunk.strip().lstrip(",").strip()
        if not chunk:
            continue
        try:
            yield json.loads("{" + chunk + "}")
        except json.JSONDecodeError:
            continue


def collect_data_images(node, out: list):
    if isinstance(node, dict):
        rid = node.get("id")
        for v in node.values():
            collect_data_images(v, out)
        if isinstance(node.get("data"), str) and node["data"].startswith("data:image"):
            out.append((str(rid) if rid else None, node["data"]))
    elif isinstance(node, list):
        for v in node:
            collect_data_images(v, out)
    elif isinstance(node, str) and node.startswith("data:image"):
        out.append((None, node))


def decode(data_url: str) -> bytes:
    header, _, b64 = data_url.partition(",")
    b64 = "".join(b64.split())
    pad = len(b64) % 4
    if pad:
        b64 += "=" * (4 - pad)
    return base64.b64decode(b64, validate=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--min-kb", type=float, default=1.0)
    ap.add_argument("--out", default="images")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    out_dir = os.path.join(root, args.out)
    os.makedirs(out_dir, exist_ok=True)
    min_bytes = int(args.min_kb * 1024)

    stats = Counter()
    manifest: list = []
    sources = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", args.out)]
        for fn in filenames:
            if fn.lower().endswith((".json", ".txt")):
                sources.append(os.path.join(dirpath, fn))

    for path in sources:
        rel = os.path.relpath(path, root)
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        found: list = []
        if path.lower().endswith(".json"):
            for obj in iter_json_objects(text):
                collect_data_images(obj, found)
        else:
            for chunk in text.replace(",", "\n").splitlines():
                chunk = chunk.strip()
                if chunk.startswith("data:image"):
                    found.append((None, chunk))
        if not found:
            continue
        stats["files_with_images"] += 1
        stem = os.path.splitext(os.path.basename(path))[0]
        for i, (rid, data_url) in enumerate(found, start=1):
            stats["inline_images"] += 1
            try:
                blob = decode(data_url)
            except (binascii.Error, ValueError):
                stats["bad_base64"] += 1
                continue
            if len(blob) < min_bytes:
                stats["too_small"] += 1
                continue
            if not blob.startswith(UA_PNG_MAGIC):
                # не PNG (бывает webp/jpeg) — сохраняем как есть
                stats["not_png"] += 1
            name = "%s.%s" % (rid or "%s_%d" % (stem, i), "png" if blob.startswith(UA_PNG_MAGIC) else "bin")
            with open(os.path.join(out_dir, name), "wb") as fh:
                fh.write(blob)
            stats["saved"] += 1
            manifest.append({"source": rel, "id": rid, "file": "%s/%s" % (args.out, name), "bytes": len(blob)})

    if manifest:
        with open(os.path.join(root, "inline-images.json"), "w", encoding="utf-8") as fh:
            json.dump({"counts": dict(stats), "images": manifest}, fh, ensure_ascii=False, indent=1)

    print("extract-inline-images: файлов с картинками %d, inline-картинок %d, сохранено %d "
          "(мелких %d, битых %d) → %s"
          % (stats["files_with_images"], stats["inline_images"], stats["saved"],
             stats["too_small"], stats["bad_base64"], out_dir))
    return 0 if stats["saved"] else 1


if __name__ == "__main__":
    sys.exit(main())
