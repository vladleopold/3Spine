#!/usr/bin/env python3
"""
Universal Pragmatic Play (UHT) asset detector & extractor.

Detects and extracts:
  1. PNG/JPG     — type:Texture + data:image/*;base64
  2. Spine JSON  — type:UHTSpine + data.spineJSON (base64) → .json skeletons
  3. Atlas       — UIAtlas.spriteList → classic Spine .atlas text
  4. Links       — SpineController spineData ↔ atlases GUIDs

No standalone .skel binary in public packs (JSON only). Output is CI-friendly KEEP lines.

Usage:
  python3 extract_pragmatic_uht.py ./resources -o ./out --symbol vs20olympgate
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Iterator


# ─── remote textures (isInline:false → res/<guid>.ktx) ─────────────

def collect_remote_textures(files: list[Path]) -> dict[str, str]:
    """guid -> относительный путь вида res/<guid>.ktx для не-inline текстур.

    У части Spine-проектов страница атласа не inline, а отдельным .ktx-файлом.
    Раньше такие страницы просто терялись — теперь мы их знаем и качаем.
    """
    out: dict[str, str] = {}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(
            r'"type"\s*:\s*"Texture"\s*,\s*"id"\s*:\s*"([0-9a-f]{32})"'
            r'\s*,\s*"isInline"\s*:\s*false\s*,\s*"data"\s*:\s*"([^"]{4,160})"',
            text,
        ):
            guid, rel = m.group(1), m.group(2)
            if rel.lower().startswith(("res/", "./")):
                out.setdefault(guid, rel)
    return out


def download_remote_textures(
    remotes: dict[str, str],
    base_urls: list[str],
    tex_dir: Path,
) -> dict[str, Path]:
    """Скачивает не-inline текстуры. base_urls пробуются по очереди."""
    got: dict[str, Path] = {}
    for guid, rel in remotes.items():
        dest = tex_dir / Path(rel).name
        if dest.exists() and dest.stat().st_size > 32:
            got[guid] = dest
            continue
        for base in base_urls:
            url = base.rstrip("/") + "/" + rel.lstrip("./")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 CI-Bot"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                if len(data) > 32:
                    dest.write_bytes(data)
                    got[guid] = dest
                    print(f"GET  remote texture {dest.name} ({len(data)} bytes) ← {base}")
                    break
            except Exception:
                continue
    return got


def ktx_size(data: bytes) -> tuple[int, int] | None:
    """Размер страницы из KTX/KTX2-заголовка.

    Идентификатор у KTX1 и KTX2 одинаковый, поэтому пробуем обе раскладки
    и берём ту, где ширина/высота правдоподобны.
    """
    if len(data) < 64 or data[:4] != b"\xabKTX":
        return None
    vals = struct.unpack_from("<16I", data, 12)

    def ok(p: tuple[int, int]) -> bool:
        return 16 <= p[0] <= 8192 and 16 <= p[1] <= 8192

    cands = [(vals[6], vals[7]), (vals[2], vals[3])]   # KTX1- и KTX2-раскладка
    for p in cands:
        if ok(p):
            return p
    return None


def transcode_ktx(path: Path) -> Path | None:
    """KTX → PNG, если в раннере есть transcoder. Иначе None."""
    out = path.with_suffix(".png")
    if out.exists() and out.stat().st_size > 32:
        return out
    # 1) готовые transcoder'ы с явным выходным файлом
    for cmd in (
        ["ktx2ktx2", "--decode", str(path)],                  # KTX-Software
        ["ktx", "--decode", str(path)],
        ["convert", str(path), str(out)],                     # ImageMagick
        ["basisu", "-ktx", str(path), "-file_out", str(out)], # Basis Universal
    ):
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        try:
            subprocess.run([exe] + cmd[1:], check=True, capture_output=True, timeout=180)
        except Exception:
            continue
        if out.exists() and out.stat().st_size > 32:
            print(f"KTX→PNG: {path.name} → {out.name} ({out.stat().st_size} bytes)")
            return out

    # 2) basisu без указания выхода: он кладёт .png рядом с исходником
    exe = shutil.which("basisu")
    if exe:
        try:
            subprocess.run([exe, "-ktx", str(path), "-y_flip"], check=True,
                           capture_output=True, timeout=180, cwd=path.parent)
        except Exception:
            pass
        for cand in (path.with_suffix(".png"), out):
            if cand.exists() and cand.stat().st_size > 32:
                if cand != out:
                    out.write_bytes(cand.read_bytes())
                    if cand != path.with_suffix(".png"):
                        cand.unlink()
                print(f"KTX→PNG: {path.name} → {out.name} ({out.stat().st_size} bytes)")
                return out
    return None


def cdn_res(symbol: str) -> str:
    return (
        "https://demogamesfree.pragmaticplay.net"
        f"/gs2c/common/v3/games-html5/games/vs/{symbol}/desktop/game/res"
    )


# ─── low-level helpers ───────────────────────────────────────────────

def iter_json_objects(text: str) -> Iterator[dict[str, Any]]:
    try:
        root = json.loads(text)
        if isinstance(root, dict) and "resources" in root:
            for item in root["resources"]:
                if isinstance(item, dict) and "type" in item:
                    yield item
            return
        if isinstance(root, list):
            for item in root:
                if isinstance(item, dict) and "type" in item:
                    yield item
            return
    except json.JSONDecodeError:
        pass
    for m in re.finditer(r'\{\s*"type"\s*:\s*"([^"]+)"', text):
        start = m.start()
        depth = 0
        in_str = esc = False
        end = start
        for i, ch in enumerate(text[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        try:
            obj = json.loads(text[start:end])
            if isinstance(obj, dict) and "type" in obj:
                yield obj
        except json.JSONDecodeError:
            continue


def decode_b64_json(b64: str) -> dict | None:
    try:
        data = json.loads(base64.b64decode(b64))
        if isinstance(data, dict) and ("skeleton" in data or "bones" in data):
            return data
    except Exception:
        return None
    return None


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
    out = stem.with_suffix(f".{ext}")
    out.write_bytes(raw)
    return out


def png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(b"\x89PNG"):
        return None
    try:
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    except Exception:
        return None


def safe_name(name: str) -> str:
    name = re.sub(r"_SkeletonData$", "", name, flags=re.I)
    name = re.sub(r"_Material$", "", name, flags=re.I)
    return re.sub(r"[^\w.\-]+", "_", name).strip("_") or "unnamed"


def sprite_list_to_atlas_multi(pages: list[tuple[str, int, int, dict]]) -> str:
    """Классический .atlas на несколько страниц (Spine это поддерживает)."""
    out: list[str] = []
    for page_name, page_w, page_h, sprite_list in pages:
        out.append(sprite_list_to_atlas(page_name, page_w, page_h, sprite_list))
    return "\n".join(out)


def sprite_list_to_atlas(page_name: str, page_w: int, page_h: int, sprite_list: dict) -> str:
    lines = [
        page_name,
        f"size: {page_w},{page_h}",
        "format: RGBA8888",
        "filter: Linear,Linear",
        "repeat: none",
    ]
    for raw_name, info in sprite_list.items():
        if not isinstance(info, dict):
            continue
        name = raw_name[2:] if raw_name.startswith("s_") else raw_name
        x = int(info.get("x") or 0)
        y = int(info.get("y") or 0)
        w = int(info.get("width") or 0)
        h = int(info.get("height") or 0)
        rotate = bool(info.get("rotate"))
        pl = int(info.get("paddingLeft") or 0)
        pt = int(info.get("paddingTop") or 0)
        pr = int(info.get("paddingRight") or 0)
        pb = int(info.get("paddingBottom") or 0)
        lines += [
            name,
            f"  rotate: {'true' if rotate else 'false'}",
            f"  xy: {x}, {y}",
            f"  size: {w}, {h}",
            f"  orig: {w + pl + pr}, {h + pt + pb}",
            f"  offset: {pl}, {pb}",
            "  index: -1",
        ]
    return "\n".join(lines) + "\n"


TEX_PATTERNS = [
    re.compile(
        r'"type"\s*:\s*"Texture"\s*,\s*"id"\s*:\s*"([a-f0-9]{32})"\s*,'
        r'\s*"isInline"\s*:\s*true\s*,\s*"data"\s*:\s*'
        r'"(data:image/(?:png|jpeg|jpg);base64,[A-Za-z0-9+/=]+)"',
        re.I,
    ),
    re.compile(
        r'"type"\s*:\s*"Texture"\s*,\s*"id"\s*:\s*"([a-f0-9]{32})"\s*,'
        r'\s*"data"\s*:\s*"(data:image/(?:png|jpeg|jpg);base64,[A-Za-z0-9+/=]+)"',
        re.I,
    ),
]


def extract_textures(files: list[Path], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    saved: list[Path] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in TEX_PATTERNS:
            for m in pat.finditer(text):
                guid, uri = m.group(1), m.group(2)
                if guid in seen:
                    continue
                seen.add(guid)
                p = save_data_uri(uri, out_dir / guid)
                if p:
                    saved.append(p)
                    print(f"KEEP: textures/{p.name} (PNG/JPG, {p.stat().st_size} bytes, base64 Texture)")
    return saved


def collect_spine_and_atlas(files: list[Path]) -> tuple[dict, dict, dict[str, set[str]]]:
    spines: dict[str, dict] = {}
    atlases: dict[str, dict] = {}
    links: dict[str, set[str]] = {}

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for obj in iter_json_objects(text):
            t = obj.get("type")
            oid = str(obj.get("id", ""))
            data = obj.get("data")

            if t == "UHTSpine" and isinstance(data, dict):
                b64 = data.get("spineJSON") or data.get("spineJson") or ""
                skel = decode_b64_json(b64) if b64 else None
                if skel:
                    spines[oid] = {"name": data.get("name") or oid, "skeleton": skel}

            elif t == "GameObject" and isinstance(data, dict):
                for go in data.get("root") or []:
                    if not isinstance(go, dict):
                        continue
                    for comp in go.get("components") or []:
                        if not isinstance(comp, dict):
                            continue
                        if comp.get("componentType") != "UIAtlas":
                            continue
                        sd = comp.get("serializableData") or {}
                        atlases[oid] = {
                            "name": go.get("name") or oid,
                            "texture_guid": (sd.get("textureContent") or {}).get("guid", ""),
                            "sprite_list": sd.get("spriteList") or {},
                        }

        for m in re.finditer(
            r'"componentType"\s*:\s*"SpineController"([\s\S]{0,4000})', text
        ):
            block = m.group(0)
            spine_guids = re.findall(
                r'"spineData"\s*:\s*\{[^}]*"guid"\s*:\s*"([a-f0-9]{32})"', block
            )
            am = re.search(r'"spineAtlases"([\s\S]{0,2500})', block)
            atlas_guids = (
                re.findall(r'"guid"\s*:\s*"([a-f0-9]{32})"', am.group(1)) if am else []
            )
            for sg in spine_guids:
                links.setdefault(sg, set()).update(
                    a for a in atlas_guids if a not in spine_guids
                )

    return spines, atlases, links


def write_spine_projects(
    spines: dict,
    atlases: dict,
    links: dict[str, set[str]],
    tex_dir: Path,
    out_dir: Path,
) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    projects: list[dict] = []

    for guid, sp in spines.items():
        name = safe_name(sp["name"])
        if name in used:
            name = f"{name}_{guid[:8]}"
        used.add(name)
        proj = out_dir / name
        proj.mkdir(parents=True, exist_ok=True)

        jp = proj / f"{name}.json"
        jp.write_text(json.dumps(sp["skeleton"], indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"KEEP: spine/{name}/{name}.json (Spine skeleton JSON, {jp.stat().st_size} bytes)")

        info: dict[str, Any] = {
            "name": name,
            "json": True,
            "atlas": False,
            "png": False,
            "ver": sp["skeleton"].get("skeleton", {}).get("spine", "?"),
        }

        # Атласов у проекта обычно несколько (страниц): берём все, что связаны,
        # а если связки нет — подбираем по имени (tbdh_freegame_tbdh_freegame).
        atlas_metas: list[dict] = []
        for ag in links.get(guid, set()):
            if ag in atlases:
                atlas_metas.append(atlases[ag])
        if not atlas_metas:
            base = name.lower()
            for a in atlases.values():
                if (a.get("name") or "").lower().startswith(base):
                    atlas_metas.append(a)
            atlas_metas.sort(key=lambda a: a.get("name") or "")

        if atlas_metas:
            pages: list[tuple[str, int, int, dict]] = []
            for idx, meta in enumerate(atlas_metas, 1):
                tex_guid = meta.get("texture_guid") or ""
                sprites = meta.get("sprite_list") or {}
                if not sprites:
                    continue
                cand = None
                for c in tex_dir.glob(f"{tex_guid}.*"):
                    cand = c
                    break
                if cand is None and tex_guid:
                    for ext in ("png", "jpg"):
                        url = f"{cdn_res(getattr(write_spine_projects, '_symbol', 'vs20olympgate'))}/{tex_guid}.{ext}"
                        try:
                            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 CI-Bot"})
                            with urllib.request.urlopen(req, timeout=20) as resp:
                                data = resp.read()
                            if len(data) > 32:
                                cand = tex_dir / f"{tex_guid}.{ext}"
                                cand.write_bytes(data)
                                break
                        except Exception:
                            pass
                if cand is None:
                    continue                      # страница не нашлась — пропускаем
                ext = cand.suffix if cand.suffix.lower() in (".png", ".jpg", ".ktx") else ".png"
                # .ktx отдаём как есть, но размер страницы всё равно нужен атласу
                page_name = f"{name}{ext}" if idx == 1 else f"{name}{idx}{ext}"
                dest = proj / page_name
                dest.write_bytes(cand.read_bytes())
                page_w, page_h = 1, 1
                sz = png_size(dest.read_bytes()) or ktx_size(dest.read_bytes())
                if sz:
                    page_w, page_h = sz
                pages.append((page_name, page_w, page_h, sprites))
                info["png"] = True
                print(f"KEEP: spine/{name}/{page_name} (Spine page PNG, {dest.stat().st_size} bytes)")
            if pages:
                ap = proj / f"{name}.atlas"
                ap.write_text(sprite_list_to_atlas_multi(pages), encoding="utf-8")
                info["atlas"] = True
                regions = sum(len(p[3]) for p in pages)
                print(f"KEEP: spine/{name}/{ap.name} (Spine atlas, {ap.stat().st_size} bytes, "
                      f"{regions} regions, {len(pages)} pages)")

        projects.append(info)
    return projects


def main() -> int:
    ap = argparse.ArgumentParser(description="Universal Pragmatic Play UHT asset extractor")
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("./uht-out"))
    ap.add_argument("--symbol", default="vs20olympgate")
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

    print(f"Symbol: {args.symbol}")
    print(f"Scanning {len(files)} JSON files…")
    print("")
    print("=== 1/3 TEXTURES (base64 → PNG) ===")
    tex_dir = args.out / "textures"
    textures = extract_textures(files, tex_dir)

    # Не-inline страницы (res/<guid>.ktx|png): раньше они просто терялись
    remotes = collect_remote_textures(files)
    if remotes:
        print(f"=== 1b/3 REMOTE PAGES (isInline:false → {len(remotes)}) ===")
        game = f"https://demogamesfree.pragmaticplay.net/gs2c/common/v3/games-html5/games/vs/{args.symbol}"
        bases = [f"{game}/desktop/game", f"{game}/mobile/game", f"{game}/desktop"]
        fetched = download_remote_textures(remotes, bases, tex_dir)
        print(f"Remote pages fetched: {len(fetched)}/{len(remotes)}")
        for p in fetched.values():
            if p.suffix.lower() == ".ktx":
                transcode_ktx(p)

    print("")
    print("=== 2/3 SPINE JSON (UHTSpine.spineJSON base64) ===")
    spines, atlases, links = collect_spine_and_atlas(files)
    print(f"Detected: UHTSpine={len(spines)}  UIAtlas={len(atlases)}  SpineController links={len(links)}")

    print("")
    print("=== 3/3 SPINE PROJECTS (json + atlas + png when available) ===")
    write_spine_projects._symbol = args.symbol  # type: ignore[attr-defined]
    projects = write_spine_projects(spines, atlases, links, tex_dir, args.out / "spine")

    pairs = sum(1 for p in projects if p["json"] and p["atlas"] and p["png"])
    json_only = sum(1 for p in projects if p["json"] and not p["atlas"])

    report = [
        "",
        "========== SUMMARY ==========",
        f"Textures PNG/JPG     : {len(textures)}",
        f"Spine skeletons JSON : {len(projects)}",
        f"  complete pairs     : {pairs}  (json+atlas+png)",
        f"  json only          : {json_only}",
        f"UIAtlas objects      : {len(atlases)}",
        "",
        "NOTE: Public UHT packs use Spine JSON (not binary .skel).",
        "      Atlas appears only when UIAtlas GameObject is in packs.",
        "      Some games ship Texture-only main_resources (no UHTSpine).",
        "",
        "Output:",
        f"  {args.out / 'textures'}/     ← PNG/JPG spritesheets",
        f"  {args.out / 'spine'}/<name>/ ← Spine project folders",
    ]
    print("\n".join(report))
    (args.out / "extract-report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")

    print("")
    print(f"FOUND textures={len(textures)} spine_json={len(projects)} pairs={pairs}")

    # Success if we got anything useful
    if not textures and not projects:
        print("ERROR: nothing extracted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
