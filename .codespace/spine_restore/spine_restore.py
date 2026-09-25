#!/usr/bin/env python3
"""
Spine Restore Tool
==================
Converts Spine 3.8 binary skeletons (.skel or misnamed .json) to readable Spine JSON.

Also detects "broken" files corrupted by UTF-8 replacement (U+FFFD / EF BF BD),
which cannot be recovered without the original binary.

Usage:
  python3 spine_restore.py file.skel
  python3 spine_restore.py file.json -o out.json
  python3 spine_restore.py ./folder -o ./output
  python3 spine_restore.py ./folder --check-only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Local import
sys.path.insert(0, str(Path(__file__).resolve().parent))
from spine38_binary_to_json import Spine38BinaryReader  # noqa: E402

FFFD = b"\xef\xbf\xbd"


def is_text_json(data: bytes) -> bool:
    s = data.lstrip()[:1]
    return s in (b"{", b"[")


def is_spine_binary(data: bytes) -> bool:
    head = data[:120]
    return b"3.8." in head or b"3.7." in head or b"4.0." in head or b"4.1." in head or b"4.2." in head


def corruption_report(data: bytes) -> dict:
    n = data.count(FFFD)
    pct = (n * 3 / max(len(data), 1)) * 100
    return {
        "fffd_count": n,
        "fffd_percent_approx": round(pct, 2),
        "corrupted": n > 0,
        "size": len(data),
    }


def convert_one(src: Path, dst: Path) -> tuple[bool, str]:
    data = src.read_bytes()

    if is_text_json(data):
        return False, "already text JSON (not binary) — skipped"

    if not is_spine_binary(data):
        return False, "not a Spine binary (no 3.x/4.x version marker)"

    report = corruption_report(data)
    if report["corrupted"]:
        return (
            False,
            f"CORRUPTED: {report['fffd_count']}× U+FFFD (~{report['fffd_percent_approx']}% of file). "
            "Original bytes were replaced by UTF-8 «replacement character». "
            "Cannot restore — re-export/re-download original binary.",
        )

    try:
        reader = Spine38BinaryReader(data)
        doc = reader.read_skeleton_data()
        leftover = len(data) - reader.pos
        if leftover > 0:
            doc.setdefault("_meta", {})["leftover_bytes"] = leftover
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        bones = len(doc.get("bones", []))
        anims = list(doc.get("animations", {}).keys())
        return True, f"OK bones={bones} anims={anims} leftover={leftover}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def collect_inputs(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    files = []
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in {".skel", ".json", ".skel.bytes"}:
            files.append(p)
    return sorted(files)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Restore/convert Spine 3.8 binary (.skel or misnamed .json) → readable JSON"
    )
    ap.add_argument("input", type=Path, help="File or folder with binary spine files")
    ap.add_argument("-o", "--output", type=Path, help="Output file or folder")
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="Only detect corruption / type, do not convert",
    )
    args = ap.parse_args()

    if not args.input.exists():
        print(f"ERROR: not found: {args.input}", file=sys.stderr)
        return 1

    inputs = collect_inputs(args.input)
    if not inputs:
        print("No .skel / .json files found.")
        return 1

    out_root = args.output
    if out_root is None:
        if args.input.is_file():
            out_root = args.input.with_name(args.input.stem + "_readable.json")
        else:
            out_root = args.input.parent / (args.input.name + "_readable")

    ok = fail = skip = 0
    for src in inputs:
        data = src.read_bytes()
        report = corruption_report(data)

        if args.check_only:
            kind = (
                "text-json"
                if is_text_json(data)
                else ("spine-binary" if is_spine_binary(data) else "unknown")
            )
            status = "CORRUPTED" if report["corrupted"] else "clean"
            print(f"{status:10} {kind:14} FFFD={report['fffd_count']:5}  {src}")
            continue

        if args.input.is_file():
            dst = out_root if out_root.suffix else out_root / (src.stem + ".json")
        else:
            rel = src.relative_to(args.input)
            dst = out_root / rel.parent / (src.stem + ".json")

        success, msg = convert_one(src, dst)
        tag = "OK  " if success else "FAIL"
        print(f"{tag} {src.name}: {msg}")
        if success:
            ok += 1
        elif "skipped" in msg or "already text" in msg or "not a Spine" in msg:
            skip += 1
        else:
            fail += 1

    if not args.check_only:
        print(f"\nDone: {ok} converted, {fail} failed, {skip} skipped")
        if isinstance(out_root, Path):
            print(f"Output: {out_root}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
