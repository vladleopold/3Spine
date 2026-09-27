#!/usr/bin/env python3
"""
Extract Spine *projects* from Pragmatic Play UHT packs.

Each UHTSpine becomes a folder:
  spine_projects/<name>/
    <name>.json     ← classic Spine skeleton
    <name>.atlas    ← classic Spine atlas text (from UIAtlas.spriteList)
    <name>.png      ← page image(s)

CI treats each folder as one Spine pair.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

def cdn_res(symbol: str) -> str:
    return (
        "https://demogamesfree.pragmaticplay.net"
        f"/gs2c/common/v3/games-html5/games/vs/{symbol}/desktop/game/res"
    )


# ---------------------------------------------------------------------------
# JSON object iterator (handles truncated multi-part packs)
# ---------------------------------------------------------------------------
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
        in_str = False
        esc = False
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


def decode_spine_json(b64: str) -> dict | None:
    try:
        data = json.loads(base64.b64decode(b64))
        if isinstance(data, dict) and ("skeleton" in data or "bones" in data):
            return data
    except Exception:
        return None
    return None


def png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(b"\x89PNG"):
        return None
    try:
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    except Exception:
        return None


def save_data_uri(payload: str, stem: Path) -> Path | None:
    m = re.match(r"data:image/(\w+);base64,(.+)", payload, re.DOTALL)
    if not m:
        return None
    ext, b64 = m.group(1), m.group(2)
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


def download_cdn(guid: str, dest_stem: Path) -> Path | None:
    for ext in ("png", "jpg"):
        out = dest_stem.with_suffix(f".{ext}")
        if out.exists() and out.stat().st_size > 32:
            return out
        url = f"{CDN_RES}/{guid}.{ext}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 CI-Bot"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
            if len(data) < 32:
                continue
            out.write_bytes(data)
            return out
        except Exception:
            continue
    return None


def safe_name(name: str) -> str:
    name = re.sub(r"_SkeletonData$", "", name, flags=re.I)
    name = re.sub(r"_Material$", "", name, flags=re.I)
    return re.sub(r"[^\w.\-]+", "_", name).strip("_") or "unnamed"


# ---------------------------------------------------------------------------
# Convert UIAtlas.spriteList → classic Spine .atlas text
# ---------------------------------------------------------------------------
def sprite_list_to_atlas(
    page_name: str,
    page_w: int,
    page_h: int,
    sprite_list: dict,
) -> str:
    """
    UHT UIAtlas fields:
      x, y, width, height, rotate (0/1),
      paddingLeft/Top/Right/Bottom (whitespace stripped at pack time)
    Spine atlas format:
      region
        rotate: true|false
        xy: x, y
        size: w, h
        orig: origW, origH
        offset: offsetX, offsetY
        index: -1
    """
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
        # strip leading s_ prefix used by UHT
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
        orig_w = w + pl + pr
        orig_h = h + pt + pb
        # Spine offset is from bottom-left of original
        off_x = pl
        off_y = pb
        lines.append(name)
        lines.append(f"  rotate: {'true' if rotate else 'false'}")
        lines.append(f"  xy: {x}, {y}")
        lines.append(f"  size: {w}, {h}")
        lines.append(f"  orig: {orig_w}, {orig_h}")
        lines.append(f"  offset: {off_x}, {off_y}")
        lines.append("  index: -1")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Collect phase: scan all files
# ---------------------------------------------------------------------------

def extract_all_base64_images(files: list[Path], tex_dir: Path) -> dict[str, Path]:
    """
    Scan raw JSON text for every data:image/*;base64 blob tied to an id.
    Returns guid -> saved Path. This is the primary image source in UHT packs.
    """
    tex_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[str, Path] = {}
    # Patterns covering common UHT orderings
    patterns = [
        re.compile(
            r'"type"\s*:\s*"Texture"\s*,\s*"id"\s*:\s*"([a-f0-9]{32})"\s*,\s*"isInline"\s*:\s*true\s*,\s*"data"\s*:\s*"(data:image/(?:png|jpeg|jpg);base64,[A-Za-z0-9+/=]+)"',
            re.I,
        ),
        re.compile(
            r'"type"\s*:\s*"Texture"\s*,\s*"id"\s*:\s*"([a-f0-9]{32})"\s*,\s*"data"\s*:\s*"(data:image/(?:png|jpeg|jpg);base64,[A-Za-z0-9+/=]+)"',
            re.I,
        ),
        re.compile(
            r'"id"\s*:\s*"([a-f0-9]{32})"\s*,\s*"isInline"\s*:\s*true\s*,\s*"data"\s*:\s*"(data:image/(?:png|jpeg|jpg);base64,[A-Za-z0-9+/=]+)"',
            re.I,
        ),
        re.compile(
            r'"id"\s*:\s*"([a-f0-9]{32})"\s*,\s*"data"\s*:\s*"(data:image/(?:png|jpeg|jpg);base64,[A-Za-z0-9+/=]{100,})"',
            re.I,
        ),
    ]
    for path in files:
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in patterns:
            for m in pat.finditer(raw):
                guid, uri = m.group(1), m.group(2)
                if guid in saved:
                    continue
                out = save_data_uri(uri, tex_dir / guid)
                if out:
                    saved[guid] = out
                    print(f"  TEXTURE   {out.name}  ({out.stat().st_size} bytes, base64)")
    return saved


def collect(files: list[Path]) -> tuple[dict, dict, dict, dict]:
    """
    Returns:
      spines:  guid -> {name, skeleton_dict}
      atlases: guid -> {name, texture_guid, sprite_list}
      textures_inline: guid -> raw image bytes path or data_uri
      spine_links: spine_guid -> set(atlas_guid)
    """
    spines: dict[str, dict] = {}
    atlases: dict[str, dict] = {}
    textures_inline: dict[str, str] = {}  # guid -> data URI
    spine_links: dict[str, set[str]] = {}

    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")

        # --- resource objects ---
        for obj in iter_json_objects(text):
            t = obj.get("type")
            oid = str(obj.get("id", ""))
            data = obj.get("data")

            if t == "UHTSpine" and isinstance(data, dict):
                b64 = data.get("spineJSON") or data.get("spineJson") or ""
                skel = decode_spine_json(b64) if b64 else None
                if skel:
                    spines[oid] = {
                        "name": data.get("name") or oid,
                        "skeleton": skel,
                    }

            elif t == "Texture":
                payload = data if isinstance(data, str) else (
                    data.get("data") if isinstance(data, dict) else None
                )
                if payload and isinstance(payload, str) and payload.startswith("data:image"):
                    textures_inline[oid] = payload

            elif t == "GameObject" and isinstance(data, dict):
                root = data.get("root") or []
                for go in root if isinstance(root, list) else []:
                    if not isinstance(go, dict):
                        continue
                    for comp in go.get("components") or []:
                        if not isinstance(comp, dict):
                            continue
                        if comp.get("componentType") != "UIAtlas":
                            continue
                        sd = comp.get("serializableData") or {}
                        sprite_list = sd.get("spriteList") or {}
                        tex_ref = (sd.get("textureContent") or {}).get("guid", "")
                        atlases[oid] = {
                            "name": go.get("name") or oid,
                            "texture_guid": tex_ref,
                            "sprite_list": sprite_list,
                        }

        # --- SpineController links (spineData guid → atlas guids) ---
        for m in re.finditer(
            r'"componentType"\s*:\s*"SpineController"([\s\S]{0,3500}?)(?="componentType"|$)',
            text,
        ):
            block = m.group(0)
            spine_guids = re.findall(
                r'"spineData"\s*:\s*\{[^}]*"guid"\s*:\s*"([a-f0-9]{32})"', block
            )
            atlas_guids = re.findall(
                r'"atlases"\s*:\s*\[[\s\S]*?"guid"\s*:\s*"([a-f0-9]{32})"', block
            )
            for sg in spine_guids:
                spine_links.setdefault(sg, set()).update(atlas_guids)

        # raw texture embeds
        for m in re.finditer(
            r'"id"\s*:\s*"([a-f0-9]{32})".{0,400}?"data"\s*:\s*"(data:image/(?:png|jpeg|jpg);base64,[A-Za-z0-9+/=]+)"',
            text,
            re.DOTALL,
        ):
            textures_inline.setdefault(m.group(1), m.group(2))

    return spines, atlases, textures_inline, spine_links


def resolve_texture(
    guid: str,
    textures_inline: dict[str, str],
    tex_cache_dir: Path,
    dest: Path,
) -> Path | None:
    """Get texture as file at dest (.png/.jpg)."""
    if dest.exists() and dest.stat().st_size > 32:
        return dest

    # inline
    if guid in textures_inline:
        saved = save_data_uri(textures_inline[guid], dest.with_suffix(""))
        if saved:
            return saved

    # already in cache dir
    for p in tex_cache_dir.glob(f"{guid}.*"):
        dest_final = dest.with_suffix(p.suffix)
        dest_final.write_bytes(p.read_bytes())
        return dest_final

    # CDN
    return download_cdn(guid, dest.with_suffix(""))


def write_project(
    out_root: Path,
    name: str,
    skeleton: dict,
    atlas_meta: dict | None,
    textures_inline: dict,
    tex_cache: Path,
) -> dict[str, Any]:
    """
    Write spine_projects/<name>/{name}.json, {name}.atlas, {name}.png
    Returns info dict for report.
    """
    proj = out_root / name
    proj.mkdir(parents=True, exist_ok=True)

    json_path = proj / f"{name}.json"
    json_path.write_text(json.dumps(skeleton, indent=2, ensure_ascii=False), encoding="utf-8")

    info: dict[str, Any] = {
        "name": name,
        "json": json_path.name,
        "atlas": None,
        "png": None,
        "spine_ver": skeleton.get("skeleton", {}).get("spine", "?"),
        "regions": 0,
    }

    if not atlas_meta:
        # still a valid skeleton-only project
        print(f"  PROJECT   {name}/  (json only, no atlas link)")
        return info

    tex_guid = atlas_meta.get("texture_guid") or ""
    sprite_list = atlas_meta.get("sprite_list") or {}
    info["regions"] = len(sprite_list)

    png_path = proj / f"{name}.png"
    resolved = resolve_texture(tex_guid, textures_inline, tex_cache, png_path) if tex_guid else None

    page_w, page_h = 1, 1
    if resolved:
        # normalize name to name.png / name.jpg inside project
        final = proj / f"{name}{resolved.suffix}"
        if resolved.resolve() != final.resolve():
            final.write_bytes(resolved.read_bytes())
        info["png"] = final.name
        sz = png_size(final.read_bytes())
        if sz:
            page_w, page_h = sz
        print(f"  PROJECT   {name}/  png={final.name} ({final.stat().st_size} bytes) regions={len(sprite_list)}")
    else:
        print(f"  PROJECT   {name}/  png=MISSING guid={tex_guid[:12] if tex_guid else '?'} regions={len(sprite_list)}")

    page_file = info["png"] or f"{name}.png"
    atlas_text = sprite_list_to_atlas(page_file, page_w, page_h, sprite_list)
    atlas_path = proj / f"{name}.atlas"
    atlas_path.write_text(atlas_text, encoding="utf-8")
    info["atlas"] = atlas_path.name

    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract Spine project pairs from UHT")
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("./uht-extracted"))
    ap.add_argument("--fetch-cdn", action="store_true")
    ap.add_argument("--symbol", default="vs20olympgate",
                    help="Pragmatic game symbol for CDN res/ path")
    args = ap.parse_args()

    global CDN_RES
    CDN_RES = cdn_res(args.symbol)
    print(f"Symbol: {args.symbol}  CDN: {CDN_RES}")

    files: list[Path] = []
    for p in args.inputs:
        if p.is_dir():
            files.extend(sorted(p.rglob("*.json")))
        elif p.is_file():
            files.append(p)
    if not files:
        print("No JSON files", file=sys.stderr)
        return 1

    print(f"Collecting from {len(files)} files…")
    spines, atlases, textures_inline, spine_links = collect(files)
    print(f"  UHTSpine: {len(spines)}  UIAtlas: {len(atlases)}  "
          f"inline tex: {len(textures_inline)}  links: {len(spine_links)}")

    out = args.out
    projects_dir = out / "spine_projects"
    tex_cache = out / "textures"
    projects_dir.mkdir(parents=True, exist_ok=True)
    tex_cache.mkdir(parents=True, exist_ok=True)

    # Primary image source: every data:image base64 Texture in the packs
    print("Extracting base64 textures (bitmap)…")
    saved_tex = extract_all_base64_images(files, tex_cache)
    # also keep collect()'s map
    for guid, uri in textures_inline.items():
        if guid not in saved_tex and isinstance(uri, str) and uri.startswith("data:image"):
            out = save_data_uri(uri, tex_cache / guid)
            if out:
                saved_tex[guid] = out
                print(f"  TEXTURE   {out.name}  ({out.stat().st_size} bytes, base64)")

    print(f"Base64 textures on disk: {len(list(tex_cache.glob('*')))}")

    # CDN fallback for atlas texture guids + known sheets
    if args.fetch_cdn:
        for a in atlases.values():
            g = a.get("texture_guid") or ""
            if g and not any(tex_cache.glob(f"{g}.*")):
                download_cdn(g, tex_cache / g)

    projects: list[dict] = []
    used_names: set[str] = set()

    for guid, sp in spines.items():
        name = safe_name(sp["name"])
        if name in used_names:
            name = f"{name}_{guid[:8]}"
        used_names.add(name)

        # pick first linked atlas that we know
        atlas_meta = None
        for ag in spine_links.get(guid, set()):
            if ag in atlases:
                atlas_meta = atlases[ag]
                break
        # fallback: atlas with matching name prefix
        if atlas_meta is None:
            prefix = name.split("_")[0] if name else ""
            for a in atlases.values():
                an = safe_name(a["name"])
                if an.startswith(name) or name.startswith(an.replace("_Material", "")):
                    atlas_meta = a
                    break

        info = write_project(
            projects_dir, name, sp["skeleton"], atlas_meta, textures_inline, tex_cache
        )
        projects.append(info)

    # report
    pairs = sum(1 for p in projects if p["atlas"] and p["png"])
    json_only = sum(1 for p in projects if not p["atlas"])
    report_lines = [
        "Spine project pairs report",
        "==========================",
        f"Projects total     : {len(projects)}",
        f"Complete pairs     : {pairs}  (json + atlas + png)",
        f"JSON only          : {json_only}",
        "",
        "Each project folder is one Spine pair:",
        "  spine_projects/<name>/<name>.json",
        "  spine_projects/<name>/<name>.atlas",
        "  spine_projects/<name>/<name>.png",
        "",
        "Projects:",
    ]
    for p in sorted(projects, key=lambda x: x["name"]):
        status = "PAIR" if p["atlas"] and p["png"] else ("ATLAS?" if p["atlas"] else "JSON")
        report_lines.append(
            f"  [{status}] {p['name']}/  spine={p['spine_ver']}  regions={p['regions']}"
        )

    report = out / "extract-report.txt"
    report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(report_lines))

    # CI KEEP lines
    print("\n=== CI KEEP ===")
    for p in sorted(projects, key=lambda x: x["name"]):
        folder = projects_dir / p["name"]
        for f in sorted(folder.iterdir()):
            kind = (
                "Spine skeleton" if f.suffix == ".json" else
                "Spine atlas" if f.suffix == ".atlas" else
                "Spine page PNG" if f.suffix in (".png", ".jpg") else
                "file"
            )
            print(f"KEEP: spine_projects/{p['name']}/{f.name} ({kind}, {f.stat().st_size} bytes)")

    if not projects:
        print("ERROR: no Spine projects", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
