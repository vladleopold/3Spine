#!/usr/bin/env python3
"""
Run the native SpineSkeletonDataConverter over a folder of .skel files and
collect converted JSON + matching images.

The converter is a Linux ELF binary and always runs inside a Linux container
(see Dockerfile / docker-compose.yml) — never as a Python re-parse.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

CONVERTER = BASE / "converter" / "SpineSkeletonDataConverter"
TIMEOUT = int(os.environ.get("SPINE_CONVERT_TIMEOUT", 120))

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tga"}


def converter_path() -> Path:
    return CONVERTER


def binary_ok() -> bool:
    return CONVERTER.is_file() and os.access(CONVERTER, os.X_OK)


def is_binary_spine(path: Path) -> bool:
    """True if file looks like a binary skeleton (not readable text JSON)."""
    try:
        with open(path, "rb") as f:
            head = f.read(128)
    except OSError:
        return False
    if head.startswith(b"{") or head.startswith(b"["):
        return False
    return b"3.8." in head or b"3.7." in head or b"4." in head or path.suffix.lower() in {".skel", ".skel.bytes"}


def extract_attachment_names(path: Path) -> list[str]:
    """Pull printable attachment-like strings from binary for image matching."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    names = re.findall(rb"[\x20-\x7e]{4,80}", data)
    result = []
    for n in names:
        s = n.decode("ascii", errors="ignore")
        if re.match(r"^\d+\.\d+", s) or len(s) < 4:
            continue
        result.append(s)
    out = []
    for s in result:
        if s not in out:
            out.append(s)
    return out


def find_images_for_skeleton(skel: Path, search_roots: list[Path]) -> list[Path]:
    names = extract_attachment_names(skel)
    base = skel.stem.lower().replace(".skel", "")
    interesting: set[str] = {base}
    for n in names:
        part = n.split("/")[-1].lower()
        interesting.add(part)
        m = re.match(r"(.+?)_?\d+$", part)
        if m:
            interesting.add(m.group(1).lower())

    found: list[Path] = []
    seen: set[str] = set()
    for root in search_roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() not in IMAGE_EXTS:
                continue
            stem = p.stem.lower()
            for key in interesting:
                if key and (key in stem or stem.startswith(key) or stem.endswith(key)):
                    r = str(p.resolve())
                    if r not in seen:
                        seen.add(r)
                        found.append(p)
                    break
    return found


def convert_one(skel: Path, out_dir: Path, logs: list[str]) -> bool:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"{skel.stem}.json"
    logs.append(f"→ {skel.name}")
    if not binary_ok():
        logs.append(f"  FAIL converter binary missing: {CONVERTER}")
        return False
    try:
        r = subprocess.run(
            [str(CONVERTER), str(skel), str(out_json)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logs.append("  FAIL timeout")
        return False
    except Exception as e:  # noqa: BLE001
        logs.append(f"  FAIL {type(e).__name__}: {e}")
        return False

    if r.returncode == 0 and out_json.is_file() and out_json.stat().st_size > 50:
        try:
            data = json.loads(out_json.read_text(encoding="utf-8"))
            out_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        logs.append(f"  OK  {skel.name} → {out_json.name}")
        return True

    err = (r.stderr or r.stdout or "").strip() or f"exit code {r.returncode}"
    logs.append(f"  FAIL {err[:220]}")
    return False


def run_conversion(work: Path, out_root: Path, logs: list[str]) -> tuple[int, int]:
    """Convert every skeleton under `work` into `out_root`, mirroring paths."""
    if not binary_ok():
        logs.append(f"ERROR: converter binary not found/executable: {CONVERTER}")
        logs.append("Run: chmod +x converter/SpineSkeletonDataConverter")
        return 0, 0

    skels = [p for p in work.rglob("*") if p.is_file() and is_binary_spine(p)]
    logs.append(f"Найдено бинарных скелетов: {len(skels)}")
    if not skels:
        logs.append("Нечего конвертировать (.skel не найдено).")
        return 0, 0

    ok = failed = 0
    for skel in skels:
        rel = skel.relative_to(work)
        dest = out_root / rel.parent / skel.stem
        if convert_one(skel, dest, logs):
            ok += 1
            roots = [skel.parent, skel.parent.parent, work]
            images = find_images_for_skeleton(skel, roots)
            if images:
                img_dir = dest / "images"
                img_dir.mkdir(exist_ok=True)
                copied = 0
                for img in images:
                    target = img_dir / img.name
                    if not target.exists():
                        try:
                            shutil.copy2(img, target)
                            copied += 1
                        except Exception:  # noqa: BLE001
                            pass
                logs.append(f"  скопировано изображений: {copied}")
        else:
            failed += 1
    return ok, failed


def main():
    if len(sys.argv) < 3:
        print(f"usage: {sys.argv[0]} <input_dir> <output_dir>")
        sys.exit(2)
    logs: list[str] = []
    ok, failed = run_conversion(Path(sys.argv[1]), Path(sys.argv[2]), logs)
    for line in logs:
        print(line)
    print(f"DONE: {ok} OK, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()