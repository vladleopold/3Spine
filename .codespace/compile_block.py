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
import struct
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
    """По умолчанию — все ядра runner'а, но не больше числа файлов."""
    raw = os.environ.get("SPINE_WORKERS", "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        raw = str(max(2, (os.cpu_count() or 4)))
    return max(1, min(int(raw), max(1, count)))


def skel_version(path: str) -> str:
    """Версия движка из заголовка бинарного .skel.

    Формат 4.x: 8 байт хеша, затем длина строки одним байтом и сама строка
    вида «4.1.17» — то есть версия лежит в начале файла текстом. Без этого
    шага редактор открывает файл своей текущей версией и ругается «Error
    reading binary skeleton data, version: 4.1.17», то есть весь бинарный
    набор уходит в никуда.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:
        return ""
    m = re.search(rb"(?<![\d.])(\d{1,2}\.\d{1,2}(?:\.\d{1,3})?)(?![\d.])", head[8:24])
    return m.group(1).decode("ascii") if m else ""


def version_candidates(ver: str) -> list[str]:
    """Варианты написания версии редактора.

    В JSON PragmaticPlay версия записана как «3.7.9.1», а лаунчер знает только
    «3.7.91» и на неизвестное написание отвечает 0x105 «The requested update
    version does not exist». Поэтому после исходной строки пробуем варианты
    со склейкой чисел хвоста: 3.7.9.1 → 3.7.91 → 3.79.1.
    """
    if not ver:
        return []
    out = [ver]
    parts = ver.split(".")
    if len(parts) >= 4 and all(p.isdigit() for p in parts):
        for k in range(len(parts) - 2, 0, -1):
            cand = ".".join(parts[:k] + ["".join(parts[k:])])
            if cand not in out:
                out.append(cand)
    return out


def per_batch() -> int:
    """Размер пачки. По умолчанию — сразу всё (0 = без разбиения)."""
    raw = env_int("SPINE_BATCH", 0)
    if raw <= 0:
        return 0
    return raw


# Журнал шага. run_all() определён выше main(), поэтому say() должен быть
# доступен на уровне модуля — иначе NameError и весь блок падает на старте.
_LOG: list[str] = []


def say(msg: str) -> None:
    print(msg)
    _LOG.append(msg)


def run_all(fn, items, workers, label):
    """Все файлы одновременно; при падении — автоматически делим пополам.

    Нужно, чтобы не терять результат из-за одного плохого файла.
    """
    if not items:
        return []
    size = per_batch()
    if size and len(items) > size:
        chunks = [items[i:i + size] for i in range(0, len(items), size)]
        out = []
        for bi, chunk in enumerate(chunks, 1):
            say(f"compile-block: {label}, пачка {bi}/{len(chunks)}: {len(chunk)} файлов")
            with ThreadPoolExecutor(max_workers=min(workers, len(chunk))) as pool:
                out.extend(pool.map(fn, chunk))
        return out
    say(f"compile-block: {label}: {len(items)} файлов одним запуском, параллельно {workers}")
    try:
        with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
            return list(pool.map(fn, items))
    except Exception as e:                                  # noqa: BLE001
        say(f"compile-block: {label} общий запуск не удался ({e}); делим пополам")
        if len(items) == 1:
            return [(items[0], False, f"FAIL: {items[0]}: {e}")]
        mid = len(items) // 2
        return run_all(fn, items[:mid], workers, label + " (1/2)") + \
            run_all(fn, items[mid:], workers, label + " (2/2)")


def _attach_web(items, web_dir):
    """Проставляет в карточках путь к web-JSON, если он выгружен. Возвращает число."""
    added = 0
    if not web_dir or not os.path.isdir(web_dir):
        return 0
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or os.path.splitext(os.path.basename(str(it.get("spine", ""))))[0]
        if not name:
            continue
        cand = os.path.join(web_dir, name + ".json")
        if os.path.isfile(cand):
            it["web"] = "previews/web/" + name + ".json"
            added += 1
    return added


def _web_index(src: str, web_dir: str) -> None:
    """Проставляет в previews/index.json путь к web-JSON, если он выгружен."""
    idx_path = os.path.join(src, "previews", "index.json")
    if not os.path.exists(idx_path):
        return
    try:
        with open(idx_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:                                  # noqa: BLE001
        print(f"compile-block: previews/index.json не читается: {e}")
        return
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return
    added = _attach_web(items, web_dir)
    if added:
        try:
            with open(idx_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            say(f"compile-block: web-JSON прописан в {added} карточек")
        except Exception as e:                              # noqa: BLE001
            print(f"compile-block: не записал web-JSON в index.json: {e}")


def main() -> None:
    zin, zout = sys.argv[1], sys.argv[2]
    spine = os.environ.get("SPINE_EDITOR") or "Spine"
    license_code = os.environ.get("SPINE_LICENSE", "")
    xmx = os.environ.get("SPINE_XMX", "") or "768"
    stdin = (license_code + "\n") if license_code else None
    tmp = tempfile.mkdtemp()
    loglines: list[str] = _LOG          # say() на уровне модуля пишет сюда
    started = time.time()

    def run(cmd: list[str], timeout: int = 1800) -> int:
        try:
            r = subprocess.run(cmd, input=stdin, capture_output=False, text=True, timeout=timeout)
            return r.returncode
        except Exception as e:
            print(f"compile-block: cmd error: {e}")
            return -1

    def run_preview(cmd: list[str], timeout: int = 40, logfile=None) -> int:
        """Экспорт кадра: короткий таймаут, иначе редактор может не завершиться."""
        use_stdin = os.environ.get("SPINE_PREVIEW_STDIN", "0") == "1"
        fh = None
        if logfile:
            try:
                fh = open(logfile, "w", encoding="utf-8", errors="replace")
            except OSError:
                fh = None
        try:
            proc = subprocess.Popen(cmd,
                                    stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
                                    stdout=fh or subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                    text=True, start_new_session=True)
        except Exception as e:
            if fh:
                try:
                    fh.close()
                except OSError:
                    pass
            print(f"compile-block: preview cmd error: {e}")
            return -1
        logfile = fh
        try:
            proc.communicate(input=(stdin if use_stdin else None), timeout=timeout)
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

    def updates_dir() -> str:
        """Каталог загруженных версий редактора (в CI это ~/.spine/updates)."""
        env = os.environ.get("SPINE_UPDATES_DIR")
        if env:
            return env
        home = os.path.expanduser("~")
        for cand in (os.path.join(home, ".spine", "updates"),
                     os.path.join(home, "Library", "Application Support", "Spine", "updates")):
            if os.path.isdir(cand):
                return cand
        return os.path.join(home, ".spine", "updates")

    broken_versions: set = set()
    retried_versions: set = set()

    def editor_installed(ver: str) -> bool:
        """Файл версии редактора существует и выглядит целым (норма — десятки МБ)."""
        if not ver:
            return True
        path = os.path.join(updates_dir(), ver)
        try:
            return os.path.isfile(path) and os.path.getsize(path) > 5 * 1024 * 1024
        except OSError:
            return False

    def editor_broken(ver: str, log_hint: str = "") -> bool:
        """Версия не запускается: помечаем, чтобы не тратить время на каждом файле."""
        if not ver:
            return False
        if ver in broken_versions:
            return True
        hint = (log_hint or "").lower()
        if ("validating update file" in hint or "classformaterror" in hint
                or "an error occurred starting" in hint or "[eof]" in hint):
            broken_versions.add(ver)
            path = os.path.join(updates_dir(), ver)
            try:
                if os.path.isfile(path) and os.path.getsize(path) < 1024:
                    os.remove(path)
                    print(f"compile-block: удалён битый файл версии {ver}")
            except OSError:
                pass
            return True
        return False

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
        # SPINE_PREVIEW_MAX — потолок на ЧИСЛО превью. 0 или пусто = не ограничиваем:
        # единственный предохранитель тогда — pv_total (общий вес превью в байтах).
        # Жёсткая цифра молча выбрасывала последние наборы у игр с 25+ скелетами.
        pv_max = int(os.environ.get("SPINE_PREVIEW_MAX", "0") or 0)
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

        def _has_anim(json_hint: str) -> bool:
            """Есть ли анимации: без них редактор нечего экспортировать (и он зависает)."""
            try:
                with open(json_hint, encoding="utf-8", errors="replace") as f:
                    d = json.load(f)
                return bool(d.get("animations"))
            except Exception:
                return False

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

        _name_index: dict = {}

        def _find_by_name(name: str) -> str:
            """Найти файл по имени в любом месте дерева (страница атласа часто в корне)."""
            root = os.path.dirname(src)
            if not _name_index:
                for r, _d, files in os.walk(root):
                    for fn in files:
                        _name_index.setdefault(fn, os.path.join(r, fn))
            return _name_index.get(name, "")

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
                                    found = _find_by_name(ln)
                                    return found
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
            got = ""
            for attempt in (1, 2):
                rc = run_preview(base_cmd() + vflag + ["-i", target, "-o", outdir, "-e", settings_path],
                                 logfile=logpath if attempt == 2 else logpath + ".1")
                for root, _d, files in os.walk(outdir):
                    for fn in sorted(files):
                        if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                            got = os.path.join(root, fn)
                            break
                    if got:
                        break
                if got or attempt == 2:
                    break
                if _has_anim(json_hint):
                    continue          # редактор мог упасть нативно — одна попытка не помешает
                break
            if not got:
                try:
                    with open(logpath, encoding="utf-8", errors="replace") as f:
                        tail = [ln for ln in f.read().split("\n") if ln.strip()][-6:]
                    print(f"compile-block: preview editor rc={rc}, файлов нет: {os.path.basename(target)}")
                    for ln in tail:
                        print("compile-block: preview editor: " + ln[:200])
                except OSError:
                    print(f"compile-block: preview editor rc={rc}, лог недоступен")
            if got:
                try:
                    shutil.move(got, want)      # сначала забрать кадр, потом чистить каталог
                    shutil.rmtree(outdir, ignore_errors=True)
                    return True
                except OSError as e:
                    print(f"compile-block: превью: не удалось забрать кадр: {e}")
                    return False
            shutil.rmtree(outdir, ignore_errors=True)
            return False

        def _shrink_preview(path: str, limit: int) -> bool:
            """Уменьшаем кадр превью до limit байт. False — ужать не вышло."""
            try:
                from PIL import Image
            except Exception:
                return False
            try:
                with Image.open(path) as src_im:
                    im = src_im.convert("RGBA")
                    for side in (1400, 1100, 900, 700, 512, 384, 256):
                        w, h = im.size
                        if max(w, h) > side:
                            im = im.resize((max(1, w * side // max(w, h)),
                                            max(1, h * side // max(w, h))),
                                           Image.LANCZOS)
                        if path.lower().endswith(".png"):
                            im.save(path, optimize=True)
                        else:
                            im.save(path, quality=82)
                        if os.path.getsize(path) <= limit:
                            return True
                return os.path.getsize(path) <= limit
            except Exception as e:                       # noqa: BLE001
                say(f"compile-block: не удалось ужать превью: {e}")
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
                if (pv_max and n >= pv_max) or preview_state["bytes"] >= pv_total:
                    return ""
            atlas_png = _atlas_png(spine_path)
            if not atlas_png:
                return ""
            can_render = bool(json_hint) and _has_anim(json_hint)
            if not can_render:
                say("compile-block: превью: у скелета нет анимаций, беру текстуру атласа")
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
                may_render = (can_render and state != "fail"
                              and preview_state["rendered"] < pv_render_max
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
                # Страница атласа у PragmaticPlay бывает на несколько мегабайт,
                # и раньше такой кадр просто выбрасывался — карточка исчезала из
                # индекса целиком. Теперь ужимаем по месту.
                if _shrink_preview(want, pv_bytes):
                    size = os.path.getsize(want)
                else:
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
            # Диагностика: сколько .skel реально дошло до этого шага
            _n_skel = sum(1 for _r, _d, _fs in os.walk(src) for _f in _fs if _f.lower().endswith(".skel"))
            _n_jso = sum(1 for _r, _d, _fs in os.walk(src) for _f in _fs if _f.lower().endswith(".json"))
            say(f"compile-block: в архиве .skel={_n_skel} .json={_n_jso}, "
                f"corrupt-list={len(known_corrupt)}")
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
                    f"все сразу, параллельно {workers}")
                if license_code:
                    say("compile-block: активация лицензии (SPINE_LICENSE задан)")

                def skel_to_json(job):
                    skel, outjson, rel = job
                    # Версию движка берём из самого бинарника и просим лаунчер
                    # именно её; иначе редактор откажется читать файл.
                    ver = skel_version(skel)
                    vers = version_candidates(ver) if ver else [""]
                    if ver and "." in ver:
                        # лаунчер умеет и семейство («4.1»), если точного патча нет
                        fam = ".".join(ver.split(".")[:2])
                        if fam not in vers:
                            vers.append(fam)
                    plans = [base_cmd() + (["-u", v] if v else [])
                             + ["-i", skel, "-o", outjson, "-e", "json"]
                             for v in vers]
                    # запасной вариант: текущий редактор без -u
                    plans.append(base_cmd() + ["-i", skel, "-o", outjson, "-e", "json"])
                    last = 0
                    for attempt in range(3):
                        for cmd in plans:
                            if os.path.exists(outjson) and os.path.getsize(outjson) > 0:
                                return rel, True, (
                                    f"compile-block: ✓ {rel} → {os.path.basename(outjson)} "
                                    f"(skel→json, редактор {ver or 'текущий'})")
                            used = cmd[cmd.index("-u") + 1] if "-u" in cmd else ""
                            if used and not editor_installed(used) and attempt == 0:
                                # первый запуск сам докачает редактор
                                pass
                            last = run(cmd)
                            if os.path.exists(outjson) and os.path.getsize(outjson) > 0:
                                return rel, True, (
                                    f"compile-block: ✓ {rel} → {os.path.basename(outjson)} "
                                    f"(skel→json, редактор {used or 'текущий'})")
                        if attempt < 2:
                            time.sleep(1.5 + attempt)
                    return rel, False, (
                        f"compile-block: FAIL {rel} (skel→json): rc={last} "
                        f"(версии: {', '.join(v or 'текущая' for v in vers)})")

                results = []
                # Прогрев одного файла перед основным заходом — только если
                # его явно попросили: по умолчанию все проекты стартуют
                # одновременно (лицензия подаётся каждому процессу через stdin)
                warmup = env_int("SPINE_WARMUP", 0)
                if warmup > 0:
                    results.extend(skel_to_json(sk) for sk in skels[:warmup])
                    rest = skels[warmup:]
                else:
                    rest = skels
                results.extend(run_all(skel_to_json, rest, workers, "ЭТАП 1"))
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
                    f"все сразу, параллельно {workers}, -Xmx{xmx}m")

                def json_to_spine(job, fallback=False):
                    p, out_spine, ver, rel = job
                    rel_spine = rel[:-5] + ".spine" if rel.lower().endswith(".json") else rel
                    tail = ["-i", p, "-o", out_spine, "-r"]
                    # если в JSON указана версия — используем ровно её:
                    # подстановка «последней» (4.3.x) ломает 3.x-кривые и тянет
                    # лишнюю загрузку редактора
                    vers = version_candidates(ver) if (ver and not fallback) else [""]
                    # первый запуск сам скачает нужную версию редактора; если
                    # написание из JSON лаунчер не знает (0x105) — идём к следующему
                    for v in vers:
                        if not v or editor_installed(v) or v in retried_versions:
                            break
                        retried_versions.add(v)
                        run(base_cmd() + ["-u", v, "-i", p, "-o", out_spine, "-r"])
                        if os.path.exists(out_spine) and os.path.getsize(out_spine) > 0:
                            return rel, True, (f"compile-block: ✓ {rel} → "
                                               f"{os.path.basename(out_spine)} (Spine {ver}, версия {v})")
                        if not editor_installed(v):
                            path = os.path.join(updates_dir(), v)
                            try:
                                if os.path.exists(path):
                                    os.remove(path)
                            except OSError:
                                pass
                            broken_versions.add(v)
                            say(f"compile-block: {rel}: редактор {v} недоступен, "
                                f"пробую другое написание версии")
                            continue
                        break
                    plans = [v for v in vers if not (v and editor_broken(v))] or [""]
                    attempts = [base_cmd() + (["-u", v] if v else []) + tail for v in plans]
                    if len(plans) == 1:
                        attempts = attempts * 2
                    rc = -1
                    for idx, cmd in enumerate(attempts):
                        used = ("версия " + cmd[cmd.index("-u") + 1]) if "-u" in cmd else "последняя"
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
                        alt_cmds = [base_cmd() + ["-u", v] + tail2 for v in plans if v]
                        alt_cmds.append(base_cmd() + tail2)
                        for cmd in alt_cmds:
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
                    return rel, False, (f"compile-block: FAIL {rel} (json→spine): rc={rc} "
                                        f"(версии: {', '.join(v or 'последняя' for v in plans)})")

                results = [json_to_spine(jobs[0])]
                rest = jobs[1:]
                results.extend(run_all(lambda j: json_to_spine(j), rest, workers, "ЭТАП 2"))
                compiled = sum(1 for _r, ok, _m in results if ok)
                for _r, ok, msg in results:
                    if ok:
                        say(msg)
                    else:
                        say(msg)
                say(f"compile-block: итого скомпилировано .spine: {compiled} из {len(jobs)}, "
                    f"время {time.time() - started:.1f}s")

            # ── ЭТАП 3: web-JSON для нативного проигрывания в браузере ──────────
            # Браузер играет скелеты рантаймом 4.x: опубликованные сборки 3.5-3.8
            # неполные (в spine-canvas 3.x нет класса рендерера), а редактор 4.3
            # на 3.8-кривых падает с "Invalid curve". Поэтому конвертируем сами.
            # Единственное расхождение форматов 3.8 и 4.x в таймлайнах слотов —
            # цвет: в 3.8 он называется "color", в 4.x "rgba". Рантайм 4.x читает
            # только "rgba", "color" отбрасывает молча, слоты остаются с альфой
            # из setup-позы (у наших скелетов часто 0) — кадр пустой, картинки нет.
            # Правило одно на все игры; .spine и исходный 3.8 JSON не трогаем.
            web_dir = os.path.join(src, "previews", "web")
            if jobs and os.environ.get("SPINE_WEB_JSON", "1") == "1":
                web_mod = None
                try:
                    import importlib.util as _ilu
                    _spec = _ilu.spec_from_file_location(
                        "spine_web_json",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "to_web_json.py"))
                    web_mod = _ilu.module_from_spec(_spec)
                    _spec.loader.exec_module(web_mod)
                except Exception as e:                       # noqa: BLE001
                    print(f"compile-block: конвертер web-JSON недоступен: {e}")

                wjobs = []
                used = set()
                for (p, out_spine, ver, rel) in jobs:
                    # 4.x больше не пропускаем: конвертер держит данные 4.x в их
                    # СВОЕМ формате (цвет слота в 4.1 снова "color", физика на
                    # месте) и только чинит скины-объекты. Раньше здесь стоял
                    # `continue` для версий 4.x, из-за чего 3Oasks не получал
                    # web-JSON вообще, а на сайте не оставалось ни одной карточки.
                    stem = os.path.splitext(os.path.basename(p))[0]
                    if stem in used:
                        stem = rel[:-5].replace("/", "_")
                    used.add(stem)
                    wjobs.append((p, rel, stem))

                if wjobs and web_mod is not None:
                    os.makedirs(web_dir, exist_ok=True)

                    def make_web(job):
                        p, rel, stem = job
                        out = os.path.join(web_dir, stem + ".json")
                        try:
                            stats = web_mod.convert_file(p, out)
                        except (OSError, ValueError) as e:      # noqa: BLE001
                            return rel, False, out
                        if not os.path.isfile(out) or os.path.getsize(out) == 0:
                            return rel, False, out
                        say(f"compile-block: ✓ {rel} → web/{stem}.json "
                            f"({web_mod.WEB_VERSION}, цветовых кадров "
                            f"{stats['кадров_цвета_в_3x']}, слотов {stats['слотов']})")
                        return rel, True, out

                    workers3 = parallel_limit(len(wjobs))
                    say(f"compile-block: ЭТАП 3 (web-JSON {web_mod.WEB_VERSION}): "
                        f"{len(wjobs)} файлов, параллельно {workers3}")
                    res3 = run_all(make_web, wjobs, workers3, "ЭТАП 3")
                    ok3 = sum(1 for _r, ok, _o in res3 if ok)
                    say(f"compile-block: ЭТАП 3 итог: {ok3}/{len(wjobs)} web-JSON, "
                        f"время {time.time() - started:.1f}s")
                    _web_index(src, web_dir)

            with open(os.path.join(src, "compile-log.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(loglines) + "\n")

        if not os.path.exists(os.path.join(src, "compile-log.txt")):
            with open(os.path.join(src, "compile-log.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(loglines) + "\n")

        if previews:
            try:
                os.makedirs(preview_root, exist_ok=True)
                # индекс переписывается целиком, поэтому web-JSON проставляем
                # ещё раз — иначе финальный дамп затирает ключи из _web_index
                nw = _attach_web(previews, os.path.join(src, "previews", "web"))
                if nw:
                    say(f"compile-block: web-JSON прописан в {nw} карточек (финальный индекс)")
                with open(os.path.join(preview_root, "index.json"), "w", encoding="utf-8") as f:
                    json.dump({"items": previews}, f, ensure_ascii=False, indent=1)
                print(f"compile-block: превью готово: {len(previews)}")
            except Exception as e:
                print(f"compile-block: индекс превью не записан: {e}")

        # отчёт: добавляем сведения о лечении
        repair_note = ""
        rep_path = os.path.join(src, "repair-report.json")
        if os.path.exists(rep_path):
            try:
                with open(rep_path, encoding="utf-8") as f:
                    rr = json.load(f)
                healed = rr.get("healed") or []
                if healed:
                    repair_note = ("\nВосстановлено лечением повреждённых скелетов: %d "
                                   "(см. repair-report.json)" % len(healed))
            except Exception:
                repair_note = ""

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
            if repair_note:
                report.append(repair_note.strip())
                report.append("")
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
