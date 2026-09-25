#!/usr/bin/env python3
# Харнесс конвертации в GitHub Codespaces (Linux).
# Использование: convert.py <input.zip> <output.zip>
# Для каждого .skel внутри входного ZIP запускает настоящий C++-конвертер,
# прикладывает сопутствующие изображения и кладёт результат в output.zip.
import os, sys, json, zipfile, shutil, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CONVERTER = os.path.join(HERE, "..", "backend", "converter", "SpineSkeletonDataConverter")
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


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
            if sk.lower().endswith(".json"):
                try:
                    with open(sk, encoding="utf-8") as f:
                        d = json.load(f)
                    is_spine_json = bool(isinstance(d, dict)
                                         and d.get("skeleton", {}).get("spine"))
                except Exception:
                    is_spine_json = False

            if sk.lower().endswith(".json") and not is_spine_json:
                shutil.copy2(sk, outjson)
                return rel, "KEEP", "KEEP: %s (не Spine-скелет, копирую как есть)" % rel, ""

            try:
                subprocess.run([CONVERTER, sk, outjson], check=True, capture_output=True)
                with open(outjson, encoding="utf-8") as f:
                    data = json.load(f)
                with open(outjson, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                return rel, "OK", "OK:   %s" % rel, ""
            except Exception as e:
                # Нативный конвертер не справился — отдаём исходный .skel дальше,
                # его доведёт до .json/.spine сам редактор Spine в compile-джобе.
                raw_out = os.path.join(dst, rel)
                os.makedirs(os.path.dirname(raw_out), exist_ok=True)
                shutil.copy2(sk, raw_out)
                return rel, "FAIL", "FAIL: %s: %s (исходник сохранён, доведёт редактор)" % (rel, e), str(e)

        if workers == 1 or len(skels) <= 1:
            results = [convert_one(sk) for sk in skels]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(convert_one, skels))

        ok = failed = kept = 0
        for rel, kind, logline, _err in results:
            if kind == "OK":
                ok += 1
            elif kind == "KEEP":
                kept += 1
            else:
                failed += 1
            print(logline)
            loglines.append(logline.split(": ", 1)[1] if kind != "KEEP" else logline.split(": ", 1)[1])

        for root, _, files in os.walk(src):
            for f in files:
                if os.path.splitext(f)[1].lower() in IMG_EXTS:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, src)
                    outpath = os.path.join(dst, rel)
                    os.makedirs(os.path.dirname(outpath), exist_ok=True)
                    shutil.copy2(full, outpath)

        loglines.append(f"done: {ok} ok, {failed} failed, {kept} kept")
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

        print(f"done: {ok} ok, {failed} failed, {kept} kept")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()