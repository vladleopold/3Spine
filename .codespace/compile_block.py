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
import re
import sys
import json
import shutil
import zipfile
import subprocess
import hashlib
import tempfile
import threading
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from safezip import safe_unzip
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

    def run_preview(cmd: list[str], timeout: int = 75, logfile=None) -> int:
        """Экспорт кадра: короткий таймаут, иначе редактор может не завершиться."""
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=logfile, stderr=subprocess.STDOUT,
                                    text=True, start_new_session=True)
        except Exception as e:
            print(f"compile-block: preview cmd error: {e}")
            return -1
        if logfile is not None:
            try:
                logfile = open(logfile, "w", encoding="utf-8", errors="replace")
            except OSError:
                logfile = subprocess.DEVNULL
        try:
            proc.communicate(input=stdin, timeout=timeout)
            return proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.communicate(timeout=10)
            except Exception:
                pass
            print(f"compile-block: preview export timeout ({timeout}s), пропускаю")
            return -1
        finally:
            if hasattr(logfile, "close"):
                try:
                    logfile.close()
                except OSError:
                    pass

    def base_cmd() -> list[str]:
        return [spine] + (["-Xmx" + xmx + "m"] if xmx else [])

    try:
        src = os.path.join(tmp, "in")
        os.makedirs(src)
        safe_unzip(zin, src)

        previews: list = []
        preview_root = os.path.join(src, "previews")
        preview_probe: dict = {}          # версия редактора -> "ok"/"fail"
        preview_state = {"count": 0, "bytes": 0, "rendered": 0}
        plock = threading.Lock()
        pv_max = int(os.environ.get("SPINE_PREVIEW_MAX", "24"))
        pv_render_max = int(os.environ.get("SPINE_PREVIEW_RENDER_MAX", "8"))
        pv_per_version = int(os.environ.get("SPINE_PREVIEW_PER_VERSION", "4"))
        pv_bytes = int(os.environ.get("SPINE_PREVIEW_BYTES", str(512 * 1024)))
        pv_total = int(os.environ.get("SPINE_PREVIEW_TOTAL_BYTES", str(8 * 1024 * 1024)))
        pv_on = os.environ.get("SPINE_PREVIEW", "1") == "1"
        _tagn = [0]

        def _settings_for(family: str, ver: str, json_hint: str) -> dict:
            """Проверенные форматы export-settings: 3.8 — FQCN ExportPng, 4.x — export-png."""
            stem, anim = _project_names(json_hint)
            if family == "3":
                return {
                    "class": "com.esotericsoftware.spine.editor.export.ExportSettings$ExportPng",
                    "exportType": "png",
                    "skeletonType": {"value": "all"},
                    "animationType": {"value": "all"},
                    "skinType": {"value": "all"},
                    "scale": 100,
                    "background": None,
                    "renderImages": True,
                    "linearFiltering": True,
                    "fps": 30,
                    "lastFrame": True,
                    "rangeStart": 0,
                    "rangeEnd": 0,
                    "pad": False,
                    "msaa": 0,
                    "compression": 6,
                }
            d = {
                "class": "export-png",
                "skeletonType": "all",
                "animationType": "single",
                "skinType": "current",
                "scale": 100,
                "background": None,
                "renderImages": True,
                "linearFiltering": True,
                "fps": 30,
                "lastFrame": True,
                "rangeStart": 0,
                "rangeEnd": 0,
                "pad": False,
                "msaa": 0,
                "compression": 6,
            }
            if stem:
                d["skeleton"] = stem
            if anim:
                d["animation"] = anim
            return d

        def _project_names(json_hint: str):
            """Имя скелета и первая анимация из исходного JSON (нужны для 4.x export)."""
            stem = anim = ""
            base = json_hint[:-5] if json_hint.lower().endswith(".json") else json_hint
            stem = os.path.basename(base)
            try:
                with open(json_hint, encoding="utf-8", errors="replace") as f:
                    d = json.load(f)
                sk = d.get("skeleton") or {}
                if isinstance(sk, dict) and sk.get("hash"):
                    pass
                stem = stem or ""
                anims = d.get("animations") or {}
                if anims:
                    anim = sorted(anims)[0]
                if sk.get("spine"):
                    pass
            except Exception:
                pass
            return stem, anim

        def _family(ver: str) -> str:
            return "3" if (ver or "").startswith("3") else "4"

        IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp", ".tiff", ".tif")

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
                if not os.path.isdir(base):
                    continue
                for candidate in (stem, stem + ".atlas"):
                    ap = os.path.join(base, candidate)
                    if os.path.isfile(ap):
                        try:
                            with open(ap, encoding="utf-8", errors="replace") as f:
                                lines = [ln.strip() for ln in f.read().split("\n") if ln.strip()]
                            for i, ln in enumerate(lines):
                                if not ln.lower().endswith(IMG_EXT):
                                    continue
                                if i + 1 < len(lines) and lines[i + 1].startswith("size:"):
                                    for root, _d, files in os.walk(base):
                                        if ln in files:
                                            return os.path.join(root, ln)
                                    return ""
                        except OSError:
                            pass
                try:
                    rest = sorted(os.listdir(base))
                except OSError:
                    rest = []
                for fn in rest:
                    if fn.lower().endswith(IMG_EXT):
                        return os.path.join(base, fn)
            return ""

        def _stage(spine_path: str, atlas_png: str, tag: str) -> str:
            """Копия проекта рядом с атласом и текстурой — редактор иначе не видит картинки."""
            stage = os.path.join(tmp, "pv-" + tag)
            shutil.rmtree(stage, ignore_errors=True)
            os.makedirs(stage, exist_ok=True)
            name = os.path.basename(spine_path)
            shutil.copyfile(spine_path, os.path.join(stage, name))
            folder = os.path.dirname(spine_path)
            try:
                listing = sorted(os.listdir(folder))
            except OSError:
                listing = []
            for fn in listing:
                if fn.lower().endswith((".atlas", ".atlas.txt")):
                    try:
                        shutil.copyfile(os.path.join(folder, fn), os.path.join(stage, fn))
                    except OSError:
                        pass
                    break
            base = os.path.basename(atlas_png)
            tgt = os.path.join(stage, base)
            if not os.path.exists(tgt):
                try:
                    shutil.copyfile(atlas_png, tgt)
                except OSError:
                    pass
            return os.path.join(stage, name)

        def _render(spine_path: str, want: str, ver: str, json_hint: str, tag: str,
                    atlas_png: str = "") -> bool:
            """Один запуск редактора: экспорт кадра в каталог, первый PNG забираем себе."""
            fam = _family(ver)
            outdir = os.path.join(preview_root, tag + "_out")
            os.makedirs(outdir, exist_ok=True)
            settings_path = os.path.join(tmp, f"preview-{os.getpid()}-{_tagn[0]}.export.json")
            logpath = os.path.join(tmp, f"preview-{os.getpid()}-{_tagn[0]}.log")
            _tagn[0] += 1
            try:
                with open(settings_path, "w", encoding="utf-8") as f:
                    json.dump(_settings_for(fam, ver, json_hint), f)
            except OSError:
                return False
            target = spine_path
            if atlas_png:
                try:
                    target = _stage(spine_path, atlas_png, tag)
                except OSError:
                    target = spine_path
            vflag = ["-u", ver] if ver else []
            rc = run_preview(base_cmd() + vflag + ["-i", target, "-o", outdir, "-e", settings_path],
                             logfile=logpath)
            if rc != 0:
                try:
                    with open(logpath, encoding="utf-8", errors="replace") as f:
                        tail = [ln for ln in f.read().split("\n") if ln.strip()][-4:]
                    for ln in tail:
                        print("compile-block: preview editor: " + ln[:200])
                except OSError:
                    pass
                shutil.rmtree(outdir, ignore_errors=True)
                return False
            try:
                for fn in sorted(os.listdir(outdir)):
                    if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                        shutil.move(os.path.join(outdir, fn), want)
                        shutil.rmtree(outdir, ignore_errors=True)
                        return True
            except OSError:
                pass
            shutil.rmtree(outdir, ignore_errors=True)
            return False

        def make_preview(spine_path: str, rel: str, ver: str = "", json_hint: str = "") -> str:
            """Кадр для галереи сайта: рендер Spine, иначе — текстура атласа."""
            if not pv_on:
                return ""
            try:
                return _make_preview(spine_path, rel, ver, json_hint)
            except Exception as e:                      # превью не должно ронять компиляцию
                print(f"compile-block: превью пропущено ({type(e).__name__}: {e})")
                return ""

        def _make_preview(spine_path: str, rel: str, ver: str, json_hint: str) -> str:
            stem = rel
            for _e in (".spine", ".json"):
                if stem.lower().endswith(_e):
                    stem = stem[:-len(_e)]
                    break
            flat = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)[:60] or "preview"
            with plock:
                n = preview_state["count"]
                if n >= pv_max or preview_state["bytes"] >= pv_total:
                    return ""
            atlas_png = _atlas_png(spine_path)
            if not atlas_png:
                return ""
            tag = f"{flat}_{hashlib.md5(stem.encode('utf-8')).hexdigest()[:6]}"
            ext = os.path.splitext(atlas_png)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                ext = ".png"
            want = os.path.join(preview_root, tag + ext)
            kind = "render"
            with plock:
                dup = os.path.exists(want)
            if not dup:
                fam_key = ver or "4.3.26"
                state = preview_probe.get(fam_key)
                may_render = (state != "fail" and preview_state["rendered"] < pv_render_max
                              and n < pv_per_version)
                if state is None and may_render:
                    with plock:
                        preview_state["rendered"] += 1
                    if _render(spine_path, want, ver, json_hint, tag, atlas_png):
                        preview_probe[fam_key] = "ok"
                        say("compile-block: превью: рендер редактора работает")
                    else:
                        preview_probe[fam_key] = "fail"
                        say("compile-block: превью: редактор не отдал кадр, беру текстуру атласа")
                elif may_render:
                    with plock:
                        preview_state["rendered"] += 1
                    if _render(spine_path, want, ver, json_hint, tag, atlas_png):
                        pass
                    else:
                        kind = "atlas"
                if not os.path.exists(want):
                    kind = "atlas"
                    shutil.copyfile(atlas_png, want)
            if not os.path.exists(want):
                return ""
            size = os.path.getsize(want)
            if size > pv_bytes:
                try:
                    os.remove(want)
                except OSError:
                    pass
                return ""
            with plock:
                preview_state["count"] = n + 1
                preview_state["bytes"] += size
                previews.append({
                    "png": "previews/" + os.path.basename(want),
                    "spine": rel if rel.lower().endswith(".spine") else rel[:-5] + ".spine",
                    "name": os.path.basename(stem),
                    "bytes": size,
                    "kind": kind,
                })
                return previews[-1]["png"]


        known_corrupt: set = set()
        has_editor = bool(shutil.which(spine)) or (os.path.exists(spine) and os.access(spine, os.X_OK))
        if not has_editor:
            say(f"compile-block: WARN Spine Editor не найден (SPINE_EDITOR={spine}) — "
                f"файлов .spine не создано, остаются JSON/.skel указанной версии")
        else:
            say("compile-block: Spine Editor: готов (путь скрыт)")

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
                    rel_spine = rel[:-5] + ".spine" if rel.lower().endswith(".json") else rel
                    tail = ["-i", p, "-o", out_spine, "-r"]
                    attempts = (([base_cmd() + ["-u", ver] + tail] * 2) if (ver and not fallback) else []) \
                        + [base_cmd() + tail] * 3
                    rc = -1
                    for idx, cmd in enumerate(attempts):
                        used = ("версия " + ver) if "-u" in cmd else "последняя"
                        rc = run(cmd)
                        if os.path.exists(out_spine) and os.path.getsize(out_spine) > 0:
                            if os.environ.get("SPINE_PREVIEW", "1") == "1":
                                png = make_preview(out_spine, rel_spine, ver, p)
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
                                    make_preview(out_spine, rel_spine, ver, alt)
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
                                    make_preview(out_spine, rel_spine, ver, p)
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
