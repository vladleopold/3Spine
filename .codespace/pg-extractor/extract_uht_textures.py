#!/usr/bin/env python3
"""
Universal Pragmatic Play (UHT) texture extractor.

Finds every inline Texture in resource JSON packs:
  { "type":"Texture", "id":"<guid>", "isInline":true,
    "data":"data:image/png;base64,...." }

Writes real PNG/JPG files. Works for any game symbol (vs20olympgate,
vs20chestcol, …) — no Spine pairing required.

CI usage:
  python3 extract_uht_textures.py path/to/**/*.json -o out/textures
"""
from __future__ import annotations

import argparse
import base64
import re
import sys
from pathlib import Path

# UHT Texture patterns (orderings vary by pack version)
PATTERNS = [
    re.compile(
        r'"type"\s*:\s*"Texture"\s*,\s*"id"\s*:\s*"([a-f0-9]{32})"\s*,'
        r'\s*"isInline"\s*:\s*true\s*,\s*"data"\s*:\s*'
        r'"(data:image/(?:png|jpeg|jpg);base64,[A-Za-z0-9+/=]+)"',
        re.I,
    ),
    re.compile(
        r'"type"\s*:\s*"Texture"\s*,\s*"id"\s*:\s*"([a-f0-9]{32})"\s*,'
        r'\s*"data"\s*:\s*'
        r'"(data:image/(?:png|jpeg|jpg);base64,[A-Za-z0-9+/=]+)"',
        re.I,
    ),
    re.compile(
        r'"id"\s*:\s*"([a-f0-9]{32})"\s*,\s*"isInline"\s*:\s*true\s*,'
        r'\s*"data"\s*:\s*'
        r'"(data:image/(?:png|jpeg|jpg);base64,[A-Za-z0-9+/=]+)"',
        re.I,
    ),
]


def save_data_uri(uri: str, stem: Path) -> Path | None:
    m = re.match(r"data:image/(\w+);base64,(.+)", uri, re.DOTALL)
    if not m:
        return None
    ext, b64 = m.group(1).lower(), m.group(2)
    if ext == "jpeg":
        ext = "jpg"
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    if len(raw) < 32:
        return None
    # magic check
    if ext == "png" and not raw.startswith(b"\x89PNG"):
        pass  # still save
    out = stem.with_suffix(f".{ext}")
    out.write_bytes(raw)
    return out


def extract_file(path: Path, out_dir: Path, seen: set[str]) -> list[Path]:
    saved: list[Path] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        print(f"WARN: cannot read {path}: {e}", file=sys.stderr)
        return saved

    for pat in PATTERNS:
        for m in pat.finditer(text):
            guid, uri = m.group(1), m.group(2)
            if guid in seen:
                continue
            seen.add(guid)
            out = save_data_uri(uri, out_dir / guid)
            if out:
                saved.append(out)
                print(
                    f"KEEP: {out.name} (image/{out.suffix.lstrip('.')}, "
                    f"{out.stat().st_size} bytes, base64 Texture)"
                )
    return saved


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract all base64 Texture PNGs from Pragmatic Play UHT JSON"
    )
    ap.add_argument("inputs", nargs="+", type=Path, help="JSON files or directories")
    ap.add_argument("-o", "--out", type=Path, default=Path("./textures"))
    args = ap.parse_args()

    files: list[Path] = []
    for p in args.inputs:
        if p.is_dir():
            files.extend(sorted(p.rglob("*.json")))
        elif p.is_file():
            files.append(p)

    if not files:
        print("ERROR: no JSON files", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    all_saved: list[Path] = []

    print(f"Scanning {len(files)} JSON files for base64 Texture…")
    for f in files:
        all_saved.extend(extract_file(f, args.out, seen))

    report = args.out / "textures-report.txt"
    lines = [
        "Pragmatic Play UHT — Texture (base64 → PNG) report",
        "==================================================",
        f"JSON files scanned : {len(files)}",
        f"Textures extracted : {len(all_saved)}",
        "",
        "Detection rule:",
        '  "type":"Texture", "id":"<32-hex>", "data":"data:image/png;base64,..."',
        "",
        "Output directory:",
        f"  {args.out.resolve()}/",
        "",
        "Files:",
    ]
    for p in sorted(all_saved, key=lambda x: x.name):
        lines.append(f"  {p.name}  ({p.stat().st_size} bytes)")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("")
    print(f"FOUND {len(all_saved)} images (PNG/JPG from base64 Texture)")
    print(f"Report: {report}")
    if not all_saved:
        print("ERROR: no textures found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
