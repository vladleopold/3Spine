#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Блок-Компиляции: скелеты → .json (fallback, если нативный конвертер не справился)
#                 и → .spine (файл редактора) той версии, что указана в JSON.
# Раскладка: первый скелет ПОСЛЕДОВАТЕЛЬНО (активация лицензии без гонок),
# остальные — ПАЧКАМИ по SPINE_BATCH (12) параллельных процессов Spine.
# Нужен лицензированный Spine Editor: env SPINE_EDITOR или "Spine" в PATH.
# Лицензия активируется автоматически (env SPINE_LICENSE, stdin).
# Использование: compile_block.py <input.zip> <output.zip>
import os
import sys
import json
import shutil
import zipfile
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "") or ""
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return default


def parallel_limit(count: int) -> int:
    return max(1, min(env_int("SPINE_WORKERS", 12), max(1, count)))


def per_batch() -> int:
    return env_int("SPINE_BATCH", 12)


def main() -> None:
    zin, zout = sys.argv[1], sys.argv[2]
    spine = os.environ.get("SPINE_EDITOR") or "Spine"
    license_code = os.environ.get("SPINE_LICENSE", "")
    xmx = os.environ.get("SPINE_XMX", "") or "768"
    stdin = (license_code + "\n") if license_code else None
    tmp = tempfile.mkdtemp()
    loglines: list[str] = []
    started = time.time()

    def say(msg: str) -> None:
        print(msg)
        loglines.append(msg)

    def run(cmd: list[str], timeout: int = 1800) -> int:
        try:
            r = subprocess.run(cmd, input=stdin, capture_output=False, text=True, timeout=timeout)
            return r.returncode
        except Exception as e:
            print(f"compile-block: cmd error: {e}")
            return -1

    def base_cmd() -> list[str]:
        return [spine] + (["-Xmx" + xmx + "m"] if xmx else [])

    try:
        src = os.path.join(tmp, "in")
        os.makedirs(src)
        with zipfile.ZipFile(zin) as z:
            for name in z.namelist():
                target = os.path.join(src, name)
                if name.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(name) as f, open(target, "wb") as o:
                        shutil.copyfileobj(f, o)

        previews: list = []
        preview_root = os.path.join(src, "previews")
        preview_variant: list = [None]
        settings_path = os.path.join(tmp, "preview.export.json")

        def _base_settings() -> dict:
            return {
                "class": "export-image",
                "format": "png",
                "singleFrame": True,
                "scale": 1,
                "width": 0,
                "height": 0,
                "padding": 0,
                "transparent": True,
                "background": None,
                "pma": False,
                "sequence": {"start": 1, "digits": 4, "prefix": "", "suffix": ""},
            }

        def _variant(i: int) -> dict:
            d = _base_settings()
            if i == 1:
                d.pop("background", None)
                d.pop("pma", None)
                d["transparent"] = True
            elif i == 2:
                d.pop("background", None)
                d.pop("pma", None)
                d.pop("padding", None)
                d["sequence"] = None
            elif i == 3:
                d["class"] = "export-png"
            elif i == 4:
                d["class"] = "com.esotericsoftware.spine.editor.export.ExportImage"
            elif i == 5:
                d["class"] = "com.esotericsoftware.spine.editor.export.ExportImage"
                d.pop("background", None)
                d.pop("pma", None)
                d["sequence"] = None
                d["singleFrame"] = True
            return d

        def _write_settings(i: int) -> bool:
            try:
                with open(settings_path, "w", encoding="utf-8") as f:
                    json.dump(_variant(i), f)
                return True
            except OSError:
                return False

        IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif")

        def _atlas_png(spine_path: str) -> str:
            """Текстура, на которую ссылается ближайший атлас (или первая картинка рядом)."""
            folder = os.path.dirname(spine_path)
            stem = os.path.splitext(os.path.basename(spine_path))[0]
            search = [folder, os.path.dirname(folder), os.path.join(folder, "images")]
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                names = []
            for fn in names:
                if fn.lower().endswith((".atlas", ".atlas.txt")):
                    stem = os.path.splitext(fn)[0]
                    break
            for base in search:
                for candidate in (stem, stem + ".atlas"):
                    ap = os.path.join(base, candidate)
                    if os.path.isfile(ap):
                        try:
                            with open(ap, encoding="utf-8", errors="replace") as f:
                                lines = [ln.strip() for ln in f.read().split("\n") if ln.strip()]
                            for i, ln in enumerate(lines):
                                if not ln.endswith(".png") and not ln.lower().endswith(IMG_EXT):
                                    continue
                                if i + 1 < len(lines) and lines[i + 1].startswith("size:"):
                                    for root, _d, files in os.walk(base):
                                        if ln in files:
                                            return os.path.join(root, ln)
                                    return ""
                        except OSError:
                            pass
                for fn in sorted(os.listdir(base)) if os.path.isdir(base) else []:
                    if fn.lower().endswith(IMG_EXT):
                        return os.path.join(base, fn)
            return ""

        def make_preview(spine_path: str, rel: str, ver: str = "") -> str:
            """Рендерит первый кадр .spine в PNG для галереи на сайте."""
            flat = rel.replace(os.sep, "__").replace("/", "__")
            for _e in (".spine", ".json"):
                if flat.lower().endswith(_e):
                    flat = flat[:-len(_e)]
                    break
            os.makedirs(preview_root, exist_ok=True)
            want = os.path.join(preview_root, flat + ".png")
            if os.path.exists(want):
                return ""
            atlas_png = _atlas_png(spine_path)
            if not atlas_png:
                return ""
            vflag = ["-u", ver] if ver else []
            variants = [preview_variant[0]] if preview_variant[0] is not None else [4, 5, 3, 0, 1, 2]
            for vi in variants:
                if os.path.exists(want):
                    break
                if not _write_settings(vi):
                    return ""
                run(base_cmd() + vflag + ["-i", spine_path, "-o", want, "-e", settings_path])
                if not os.path.exists(want):
                    outdir = os.path.join(preview_root, flat + "_dir")
                    os.makedirs(outdir, exist_ok=True)
                    run(base_cmd() + vflag + ["-i", spine_path, "-o", outdir, "-e", settings_path])
                    for fn in sorted(os.listdir(outdir)):
                        if fn.lower().endswith(".png"):
                            shutil.move(os.path.join(outdir, fn), want)
                            break
                    shutil.rmtree(outdir, ignore_errors=True)
                if os.path.exists(want) and preview_variant[0] is None:
                    preview_variant[0] = vi
                    say(f"compile-block: превью: рабочий формат export-settings #{vi}")
            kind = "render"
            if not os.path.exists(want):
                shutil.copyfile(atlas_png, want)
                kind = "atlas"
            if not os.path.exists(want):
                return ""
            entry = {
                "png": "previews/" + os.path.basename(want),
                "spine": rel,
                "name": flat,
                "bytes": os.path.getsize(want),
                "kind": kind,
            }
            previews.append(entry)
            return entry["png"]

        has_editor = bool(shutil.which(spine)) or (os.path.exists(spine) and os.access(spine, os.X_OK))
        if not has_editor:
            say(f"compile-block: WARN Spine Editor не найден (SPINE_EDITOR={spine}) — "
                f"файлов .spine не создано, остаются JSON/.skel указанной версии")
        else:
            say(f"compile-block: Spine Editor: {spine}")

            # ── ЭТАП 1: .skel без пары .json → экспорт в .json редактором ──────────
            # файлы, помеченные детектором как битые, пропускаем: редактор их всё равно
            # не прочитает, а попытки стоят минут
            known_corrupt = set()
            clist = os.path.join(src, "corrupt-list.txt")
            if os.path.exists(clist):
                with open(clist, encoding="utf-8") as f:
                    known_corrupt = {ln.strip().replace("\\", "/") for ln in f if ln.strip()}

            skels = []
            for root, _, files in os.walk(src):
                for f in sorted(files):
                    if not f.lower().endswith(".skel"):
                        continue
                    p = os.path.join(root, f)
                    base = os.path.splitext(p)[0]
                    rel = os.path.relpath(p, src).replace("\\", "/")
                    if not os.path.exists(base + ".json"):
                        if rel in known_corrupt:
                            say(f"compile-block: пропускаю битый файл (детектор): {rel}")
                            continue
                        skels.append((p, base + ".json", rel))
            skels.sort(key=lambda j: j[2])

            if skels:
                workers = parallel_limit(len(skels))
                say(f"compile-block: ЭТАП 1 (skel→json): {len(skels)} файлов, "
                    f"пачками по {per_batch()}, параллельно {workers}")
                if license_code:
                    say("compile-block: активация лицензии (SPINE_LICENSE задан)")

                def skel_to_json(job):
                    skel, outjson, rel = job
                    cmd = base_cmd() + ["-i", skel, "-o", outjson, "-e", "json"]
                    for attempt in range(3):
                        rc = run(cmd)
                        if os.path.exists(outjson) and os.path.getsize(outjson) > 0:
                            return rel, True, f"compile-block: ✓ {rel} → {os.path.basename(outjson)} (skel→json, редактор)"
                        if attempt < 2:
                            time.sleep(1.5 + attempt)
                    return rel, False, f"compile-block: FAIL {rel} (skel→json): rc={rc}"

                results = []
                results.append(skel_to_json(skels[0]))
                rest = skels[1:]
                batches = [rest[i:i + per_batch()] for i in range(0, len(rest), per_batch())] or []
                for bi, batch in enumerate(batches, 1):
                    say(f"compile-block: ЭТАП 1, пачка {bi}/{len(batches)}: {len(batch)} файлов")
                    with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as pool:
                        results.extend(pool.map(skel_to_json, batch))
                ok1 = sum(1 for _r, ok, _m in results if ok)
                for _r, ok, msg in results:
                    if not ok:
                        say(msg)
                say(f"compile-block: ЭТАП 1 итог: {ok1}/{len(skels)} skel→json, {time.time() - started:.1f}s")

            # ── ЭТАП 2: .json → .spine ──────────────────────────────────────────
            restore_tool = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "spine_restore", "spine_restore.py")
            native = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "backend", "converter", "SpineSkeletonDataConverter")

            def regen_with_other_engine(src_json: str) -> str:
                """Редактор не взял JSON — перегенерируем его другим движком."""
                base = os.path.splitext(src_json)[0]
                skel = base + ".skel"
                if not os.path.exists(skel):
                    return ""
                alt = base + ".alt.json"
                if os.path.exists(alt):
                    os.remove(alt)
                # приоритет: Spine Restore Tool, затем нативный
                if os.path.exists(restore_tool):
                    try:
                        r = subprocess.run([sys.executable, restore_tool, skel, "-o", alt],
                                           capture_output=True, text=True, timeout=900,
                                           input=(license_code + "\n") if license_code else None)
                        if r.returncode == 0 and os.path.exists(alt) and os.path.getsize(alt) > 0:
                            return alt
                    except Exception:
                        pass
                if os.path.exists(native) and os.access(native, os.X_OK):
                    try:
                        subprocess.run([native, skel, alt], capture_output=True, timeout=900)
                        if os.path.exists(alt) and os.path.getsize(alt) > 0:
                            return alt
                    except Exception:
                        pass
                return ""

            # 3.x-кривые ("curve": число) редактор 4.x не принимает — приводим к stepped
            norm_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "normalize_for_editor.py")
            if os.path.exists(norm_script):
                try:
                    r = subprocess.run([sys.executable, norm_script, src],
                                       capture_output=True, text=True, timeout=600)
                    if r.stdout:
                        say("compile-block: " + r.stdout.strip())
                except Exception as e:
                    print(f"compile-block: normalize не выполнен: {e}")

            jobs = []
            for root, _, files in os.walk(src):
                for f in sorted(files):
                    if not f.lower().endswith(".json"):
                        continue
                    p = os.path.join(root, f)
                    try:
                        with open(p, encoding="utf-8") as fh:
                            d = json.load(fh)
                    except Exception:
                        continue
                    ver = ""
                    if isinstance(d, dict):
                        sk = d.get("skeleton")
                        if isinstance(sk, dict):
                            ver = sk.get("spine", "") or ""
                    if not ver:
                        continue
                    jobs.append((p, os.path.splitext(p)[0] + ".spine", ver, os.path.relpath(p, src)))
            jobs.sort(key=lambda j: j[3])

            compiled = 0
            if jobs:
                workers = parallel_limit(len(jobs))
                say(f"compile-block: ЭТАП 2 (json→spine): {len(jobs)} файлов, "
                    f"пачками по {per_batch()}, параллельно {workers}, -Xmx{xmx}m")

                def json_to_spine(job, fallback=False):
                    p, out_spine, ver, rel = job
                    tail = ["-i", p, "-o", out_spine, "-r"]
                    attempts = (([base_cmd() + ["-u", ver] + tail] * 2) if (ver and not fallback) else []) \
                        + [base_cmd() + tail] * 3
                    rc = -1
                    for idx, cmd in enumerate(attempts):
                        used = ("версия " + ver) if "-u" in cmd else "последняя"
                        rc = run(cmd)
                        if os.path.exists(out_spine) and os.path.getsize(out_spine) > 0:
                            if os.environ.get("SPINE_PREVIEW", "1") == "1":
                                png = make_preview(out_spine, rel, ver)
                                if png:
                                    print(f"compile-block: preview {png}")
                            return rel, True, f"compile-block: ✓ {rel} → {os.path.basename(out_spine)} (Spine {ver}, {used})"
                        if idx < len(attempts) - 1:
                            time.sleep(1.5 + idx)

                    # запасной путь: другой движок → другой JSON → снова в редактор
                    alt = regen_with_other_engine(p)
                    if alt:
                        say(f"compile-block: {rel}: редактор не принял JSON, пробую alt из другого движка")
                        tail2 = ["-i", alt, "-o", out_spine, "-r"]
                        for cmd in [base_cmd() + ["-u", ver] + tail2, base_cmd() + tail2]:
                            rc = run(cmd)
                            if os.path.exists(out_spine) and os.path.getsize(out_spine) > 0:
                                if os.environ.get("SPINE_PREVIEW", "1") == "1":
                                    make_preview(out_spine, rel, ver)
                                try:
                                    os.remove(alt)
                                except OSError:
                                    pass
                                return rel, True, (f"compile-block: ✓ {rel} → "
                                                   f"{os.path.basename(out_spine)} (Spine {ver}, alt-JSON)")
                        try:
                            os.remove(alt)
                        except OSError:
                            pass

                    # последний рубеж: 3.x-кривые → 4.x-массив (если импортирует 4.x)
                    try:
                        import importlib.util as _ilu
                        _spec = _ilu.spec_from_file_location("nf", norm_script)
                        _mod = _ilu.module_from_spec(_spec)
                        _spec.loader.exec_module(_mod)
                        v4 = p[:-5] + ".v4.json"
                        nfix = _mod.to_4x_curves(p, v4)
                        if nfix:
                            say(f"compile-block: {rel}: {nfix} кривых переведены в формат 4.x")
                            rc = run(base_cmd() + ["-i", v4, "-o", out_spine, "-r"])
                            if os.path.exists(out_spine) and os.path.getsize(out_spine) > 0:
                                if os.environ.get("SPINE_PREVIEW", "1") == "1":
                                    make_preview(out_spine, rel)
                                return rel, True, (f"compile-block: ✓ {rel} → "
                                                   f"{os.path.basename(out_spine)} (Spine {ver}, 4.x-кривые)")
                        for leftover in (v4,):
                            if os.path.exists(leftover):
                                try:
                                    os.remove(leftover)
                                except OSError:
                                    pass
                    except Exception as e:
                        print(f"compile-block: 4.x-конверсия не удалась: {e}")
                    return rel, False, f"compile-block: FAIL {rel} (json→spine): rc={rc}"

                results = [json_to_spine(jobs[0])]
                rest = jobs[1:]
                batches = [rest[i:i + per_batch()] for i in range(0, len(rest), per_batch())] or []
                for bi, batch in enumerate(batches, 1):
                    say(f"compile-block: ЭТАП 2, пачка {bi}/{len(batches)}: {len(batch)} файлов")
                    with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as pool:
                        results.extend(pool.map(lambda j: json_to_spine(j, True), batch))
                compiled = sum(1 for _r, ok, _m in results if ok)
                for _r, ok, msg in results:
                    if ok:
                        say(msg)
                    else:
                        say(msg)
                say(f"compile-block: итого скомпилировано .spine: {compiled} из {len(jobs)}, "
                    f"время {time.time() - started:.1f}s")

            with open(os.path.join(src, "compile-log.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(loglines) + "\n")

        if not os.path.exists(os.path.join(src, "compile-log.txt")):
            with open(os.path.join(src, "compile-log.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(loglines) + "\n")

        if previews:
            try:
                os.makedirs(preview_root, exist_ok=True)
                with open(os.path.join(preview_root, "index.json"), "w", encoding="utf-8") as f:
                    json.dump({"items": previews}, f, ensure_ascii=False, indent=1)
                print(f"compile-block: превью готово: {len(previews)}")
            except Exception as e:
                print(f"compile-block: индекс превью не записан: {e}")

        # человекочитаемый отчёт по прогону
        try:
            spine_n = sum(1 for r, _d, fs2 in os.walk(src) for f2 in fs2 if f2.endswith(".spine"))
            json_n = sum(1 for r, _d, fs2 in os.walk(src) for f2 in fs2 if f2.endswith(".json"))
            skel_n = sum(1 for r, _d, fs2 in os.walk(src) for f2 in fs2 if f2.endswith(".skel"))
            img_n = sum(1 for r, _d, fs2 in os.walk(src) for f2 in fs2
                        if f2.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp")))
            report = [
                "Spine Converter — отчёт о конвертации",
                "=" * 44,
                "",
                f"Файлов .spine (проекты редактора): {spine_n}",
                f"Файлов .json (данные Spine):        {json_n}",
                f"Файлов .skel (исходные бинарники):   {skel_n}",
                f"Картинок после распаковки атласов:   {img_n}",
                "",
            ]
            if known_corrupt:
                report.append(f"БИТЫЕ ФАЙЛЫ ({len(known_corrupt)}) — перезаписаны текстовым режимом,")
                report.append("исходные байты утеряны, конвертация невозможна. Нужен чистый архив:")
                report.extend("  " + p for p in sorted(known_corrupt))
                report.append("")
            report.append("Логи: prepare-log.txt, convert-log.txt, compile-log.txt, corrupt-list.txt")
            with open(os.path.join(src, "REPORT.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(report) + "\n")
        except Exception as e:
            print(f"compile-block: отчёт не собран: {e}")

        with zipfile.ZipFile(zout, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(src):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, src))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
