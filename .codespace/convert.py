#!/usr/bin/env python3
# Харнесс конвертации в GitHub Codespaces (Linux).
# Использование: convert.py <input.zip> <output.zip>
# Для каждого .skel внутри входного ZIP запускает настоящий C++-конвертер,
# прикладывает сопутствующие изображения и кладёт результат в output.zip.
import os, re, sys, json, zipfile, shutil, subprocess, tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from skeleton_router import classify, engine_order  # анализатор-детектор  # noqa: E402
CONVERTER = os.path.join(HERE, "..", "backend", "converter", "SpineSkeletonDataConverter")
RESTORE = os.path.join(HERE, "spine_restore", "spine_restore.py")
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


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
        detail = (r.stdout or r.stderr or "").strip().splitlines()
        return False, (detail[-1] if detail else "rc=%d" % r.returncode)
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

        with zipfile.ZipFile(zin) as z:
            for name in z.namelist():
                target = os.path.join(src, name)
                if name.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(name) as f, open(target, "wb") as o:
                        shutil.copyfileobj(f, o)

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
            raw_out = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(raw_out), exist_ok=True)
            shutil.copy2(sk, raw_out)
            reason = " | ".join(errors)[:300]
            return rel, "FAIL", "FAIL: %s [%s] %s — исходник сохранён" % (
                rel, info["kind"], reason), reason

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
                if os.path.splitext(f)[1].lower() in IMG_EXTS:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, src)
                    outpath = os.path.join(dst, rel)
                    os.makedirs(os.path.dirname(outpath), exist_ok=True)
                    shutil.copy2(full, outpath)

        loglines.append(f"done: {ok} ok, {editor} в редактор, {failed} failed, {kept} kept")
        with open(os.path.join(dst, "convert-log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(loglines) + "\n")
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

        print(f"done: {ok} ok, {editor} в редактор, {failed} failed, {kept} kept")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()