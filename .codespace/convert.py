#!/usr/bin/env python3
# Харнесс конвертации в GitHub Codespaces (Linux).
# Использование: convert.py <input.zip> <output.zip>
# Для каждого .skel внутри входного ZIP запускает настоящий C++-конвертер,
# прикладывает сопутствующие изображения и кладёт результат в output.zip.
import os, re, sys, json, time, zipfile, shutil, subprocess, tempfile, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from safezip import safe_unzip
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from skeleton_router import classify, engine_order  # анализатор-детектор  # noqa: E402
CONVERTER = os.path.join(HERE, "..", "backend", "converter", "SpineSkeletonDataConverter")
RESTORE = os.path.join(HERE, "spine_restore", "spine_restore.py")
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
# готовые проекты Spine и сопутствующие данные — пробрасываем как есть
PASSTHROUGH_EXTS = {".spine", ".bytes", ".atlas", ".xml", ".txt", ".css", ".mp3", ".wav", ".ogg", ".ttf", ".woff"}


def pretty_json(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def looks_like_binary_spine(path: str) -> bool:
    """True, если .json на самом деле бинарный Spine-скелет."""
    try:
        with open(path, "rb") as f:
            head = f.read(128)
    except OSError:
        return False
    if head.startswith((b"{", b"[")):
        return False
    if not 1 <= head[0] <= 64:
        return False
    m = re.search(rb"\d+\.\d+(\.\d+)?", head)
    if not m:
        return False
    p = m.start()
    return p >= 2 and 1 <= head[p - 1] <= 32


def convert_with_native(sk: str, outjson: str) -> tuple:
    """Движок 1: нативный C++ конвертер."""
    try:
        subprocess.run([CONVERTER, sk, outjson], check=True, capture_output=True)
        pretty_json(outjson)
        return True, "native"
    except Exception as e:
        return False, str(e)


def convert_with_restore(sk: str, outjson: str) -> tuple:
    """Движок 2: Spine Restore Tool (Python-парсер бинарников Spine 3.8)."""
    if not os.path.exists(RESTORE):
        return False, "restore-tool не найден"
    try:
        r = subprocess.run([sys.executable, RESTORE, sk, "-o", outjson],
                           capture_output=True, text=True, timeout=900)
        if r.returncode == 0 and os.path.exists(outjson) and os.path.getsize(outjson) > 0:
            pretty_json(outjson)
            return True, "restore-tool"
        lines = [ln.strip() for ln in ((r.stdout or "") + "\n" + (r.stderr or "")).splitlines() if ln.strip()]
        # берём содержательную строку: с причиной, а не служебный "Output: …"
        reason = ""
        for ln in lines:
            if ln.startswith("Output:") or ln.startswith("Done:") or ln.startswith("No "):
                continue
            reason = ln
            break
        if not reason:
            reason = lines[-1] if lines else ("rc=%d" % r.returncode)
        return False, reason[:200]
    except Exception as e:
        return False, str(e)


def main():
    zin, zout = sys.argv[1], sys.argv[2]
    tmp = tempfile.mkdtemp()
    loglines = []
    try:
        src = os.path.join(tmp, "in")
        dst = os.path.join(tmp, "out")
        os.makedirs(src)
        os.makedirs(dst)

        safe_unzip(zin, src)

        skels = []
        for root, _, files in os.walk(src):
            for f in files:
                if f.lower().endswith((".skel", ".json")):
                    skels.append(os.path.join(root, f))
        skels.sort()

        workers = int(os.environ.get("CONVERT_WORKERS", "0") or 0)
        if workers <= 0:
            workers = min(8, max(2, os.cpu_count() or 2))
        workers = max(1, min(workers, max(1, len(skels))))

        repair_state_lock = threading.Lock()

        def convert_one(sk: str) -> tuple:
            rel = os.path.relpath(sk, src)
            if sk.lower().endswith(".json"):
                outjson = os.path.join(dst, rel)
            else:
                outjson = os.path.join(dst, os.path.splitext(rel)[0] + ".json")
            os.makedirs(os.path.dirname(outjson), exist_ok=True)

            is_spine_json = False
            binary_spine = False
            if sk.lower().endswith(".json"):
                try:
                    with open(sk, encoding="utf-8") as f:
                        d = json.load(f)
                    is_spine_json = bool(isinstance(d, dict)
                                         and d.get("skeleton", {}).get("spine"))
                except Exception:
                    # не текстовый JSON — возможно это бинарный skeleton
                    binary_spine = looks_like_binary_spine(sk)

            # ── Детектор решает, какой движок вести этот файл ────────────────
            # binary-clean  → нативный C++ конвертер
            # binary-corrupt → Spine Restore Tool (нативный на таком материале
            #                  заведомо не даёт результата)
            # text-json     → только переформатирование
            info = classify(sk)
            order = engine_order(info["kind"], info["version"])
            if info["kind"] == "text-json":
                try:
                    pretty_json(sk)
                    shutil.copy2(sk, outjson)
                    return rel, "OK", "OK:   %s [уже JSON %s]" % (rel, info["version"]), ""
                except Exception as e:
                    return rel, "FAIL", "FAIL: %s: %s" % (rel, e), str(e)

            if info["kind"] == "unknown" and sk.lower().endswith(".json"):
                shutil.copy2(sk, outjson)
                return rel, "KEEP", "KEEP: %s (не Spine-скелет, копирую как есть)" % rel, ""

            if not order:
                # формат 4.x: читает сам редактор Spine — отдаём исходник в compile-этап
                raw_out = os.path.join(dst, rel)
                os.makedirs(os.path.dirname(raw_out), exist_ok=True)
                shutil.copy2(sk, raw_out)
                return rel, "EDITOR", "EDITOR: %s [4.x → редактор Spine] исходник сохранён" % rel, ""

            tmp_native = outjson[:-5] + ".native.tmp.json"
            tmp_restore = outjson[:-5] + ".restore.tmp.json"
            engines = {
                "native": lambda: convert_with_native(sk, tmp_native),
                "restore-tool": lambda: convert_with_restore(sk, tmp_restore),
            }
            errors = []
            winner = None
            used = None
            for name in order:
                tmp = tmp_native if name == "native" else tmp_restore
                try:
                    good, detail = engines[name]()
                except Exception as e:
                    good, detail = False, str(e)
                if good and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                    winner = tmp
                    used = name
                    break
                errors.append(f"{name}: {detail}")

            if winner:
                shutil.move(winner, outjson)
                for tmp in (tmp_native, tmp_restore):
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                # исходный бинарник оставляем рядом: если редактор не примет
                # этот JSON, compile-этап перегенерирует его другим движком
                if not sk.lower().endswith(".json"):
                    raw_out = os.path.join(dst, rel)
                    os.makedirs(os.path.dirname(raw_out), exist_ok=True)
                    shutil.copy2(sk, raw_out)
                route = info["kind"] + "/" + used
                return rel, "OK", "OK:   %s [%s]" % (rel, route), ""

            for tmp in (tmp_native, tmp_restore):
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            if info["kind"] == "binary-corrupt":
                line = try_repair(sk, rel, outjson)
                if line:
                    return rel, "OK", line, ""
            raw_out = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(raw_out), exist_ok=True)
            shutil.copy2(sk, raw_out)
            reason = " | ".join(errors)[:300]
            return rel, "FAIL", "FAIL: %s [%s] %s — исходник сохранён" % (
                rel, info["kind"], reason), reason

        # ── лечение битых скелетов (по желанию) ────────────────────────────
        repair_on = os.environ.get("SPINE_REPAIR", "0") == "1"
        repair_budget = int(os.environ.get("SPINE_REPAIR_BUDGET", "1500"))
        repair_min_conf = float(os.environ.get("SPINE_REPAIR_MIN_CONFIDENCE", "0.5"))
        repair_max_files = int(os.environ.get("SPINE_REPAIR_MAX_FILES", "60"))
        repair_time_limit = float(os.environ.get("SPINE_REPAIR_TIME_LIMIT", "30"))
        repair_deadline = time.monotonic() + repair_time_limit
        repair_state = {"on": repair_on, "left": repair_max_files, "done": [], "fail": []}
        repair_tool = None
        if repair_on:
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import skeleton_repair
                repair_tool = skeleton_repair
                loglines.append("repair: лечение битых скелетов включено "
                                f"(бюджет {repair_budget}, мин. уверенность {repair_min_conf}, "
                                f"макс. файлов {repair_max_files})")
            except Exception as e:
                repair_tool = None
                loglines.append(f"repair: модуль лечения недоступен: {e}")

        def try_repair(sk: str, rel: str, out_json: str) -> str:
            """Пытается вылечить битый бинарник. Возвращает пусто при неудаче."""
            if repair_tool is None:
                return ""
            with repair_state_lock:
                if repair_state["left"] <= 0:
                    return ""
                if time.monotonic() > repair_deadline:
                    repair_state.setdefault("skipped_time", 0)
                    repair_state["skipped_time"] += 1
                    return ""
                repair_state["left"] -= 1
            try:
                with open(sk, "rb") as f:
                    data = f.read()
            except OSError:
                return ""
            # бюджет обратно пропорционален размеру: крупные файлы иначе
            # съедают весь лимит времени, не давая попробовать мелкие
            scale = 40000.0 / max(40000, len(data))
            budget_here = max(200, int(repair_budget * scale))
            try:
                parsed, rep = repair_tool.heal(data, budget=budget_here,
                                                min_unknown_pct=0.0)
            except Exception as e:
                repair_state["fail"].append({"file": rel, "error": f"{type(e).__name__}: {e}"})
                return ""
            if parsed is None or rep.get("confidence", 0) < repair_min_conf:
                repair_state["fail"].append({
                    "file": rel, "error": rep.get("error", "не восстановлен"),
                    "confidence": rep.get("confidence", 0)})
                return ""
            os.makedirs(os.path.dirname(out_json), exist_ok=True)
            with open(out_json, "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False, indent=1)
            # исходный бинарник рядом — по нему видно, что файл был повреждён
            raw_out = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(raw_out), exist_ok=True)
            if not os.path.exists(raw_out):
                shutil.copy2(sk, raw_out)
            entry = {
                "file": rel,
                "confidence": rep.get("confidence", 0),
                "fffd_pct": rep.get("fffd_pct", 0),
                "bytes": rep.get("bytes", 0),
                "bones": rep.get("bones", 0),
                "slots": rep.get("slots", 0),
                "animations": rep.get("animations", 0),
                "skins": rep.get("skins", 0),
                "decisions": rep.get("decisions", []),
            }
            repair_state["done"].append(entry)
            return ("OK:   %s [лечение, уверенность %.2f, повреждено %.1f%%, "
                    "костей %d, анимаций %d]" % (rel, entry["confidence"],
                                                 entry["fffd_pct"], entry["bones"],
                                                 entry["animations"]))

        if workers == 1 or len(skels) <= 1:
            results = [convert_one(sk) for sk in skels]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(convert_one, skels))

        ok = failed = kept = editor = 0
        corrupt = []
        for rel, kind, logline, _err in results:
            if kind == "OK":
                ok += 1
            elif kind == "KEEP":
                kept += 1
            elif kind == "EDITOR":
                editor += 1
            else:
                failed += 1
            if kind == "FAIL" and "binary-corrupt" in logline:
                corrupt.append(rel)
            print(logline)
            loglines.append(logline)

        for root, _, files in os.walk(src):
            for f in files:
                if os.path.splitext(f)[1].lower() in (IMG_EXTS | PASSTHROUGH_EXTS):
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, src)
                    outpath = os.path.join(dst, rel)
                    os.makedirs(os.path.dirname(outpath), exist_ok=True)
                    shutil.copy2(full, outpath)

        _extra = (f", из них восстановлено лечением: {len(repair_state['done'])}"
                  if repair_state.get("done") else "")
        _done_line = f"done: {ok} ok, {editor} в редактор, {failed} failed, {kept} kept{_extra}"
        print(_done_line)
        loglines.append(_done_line)
        with open(os.path.join(dst, "convert-log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(loglines) + "\n")
        if repair_on and repair_tool is not None:
            rep_path = os.path.join(dst, "repair-report.json")
            with open(rep_path, "w", encoding="utf-8") as f:
                json.dump({
                    "healed": repair_state["done"],
                    "failed": repair_state["fail"],
                    "budget": repair_budget,
                    "min_confidence": repair_min_conf,
                    "max_files": repair_max_files,
                }, f, ensure_ascii=False, indent=1)
            line = (f"repair: восстановлено {len(repair_state['done'])}, "
                    f"не поддалось {len(repair_state['fail'])}")
            if repair_state.get("skipped_time"):
                line += f", пропущено по времени: {repair_state['skipped_time']}"
            print(line)
            loglines.append(line)
            if repair_state["done"]:
                line2 = ("repair: детали: " + ", ".join(
                    "%s (ув.%.2f)" % (os.path.basename(e["file"]), e["confidence"])
                    for e in repair_state["done"][:12]))
                if len(repair_state["done"]) > 12:
                    line2 += " …"
                print(line2)
                loglines.append(line2)

        if corrupt:
            with open(os.path.join(dst, "corrupt-list.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(set(corrupt))) + "\n")
            loglines.append(f"corrupt-list: {len(set(corrupt))} файлов помечены детектором как битые")
            with open(os.path.join(dst, "convert-log.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(loglines) + "\n")

        # переносим логи предыдущих блоков (prepare/unpack), чтобы причины
        # пропусков были видны в выданном архиве
        for f in os.listdir(src):
            if f.endswith(".txt") and f != "convert-log.txt":
                try:
                    shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
                except OSError:
                    pass

        with zipfile.ZipFile(zout, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(dst):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, dst)
                    z.write(full, rel)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()