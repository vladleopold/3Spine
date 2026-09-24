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
                if f.lower().endswith(".skel"):
                    skels.append(os.path.join(root, f))

        ok = failed = 0
        for sk in sorted(skels):
            rel = os.path.relpath(sk, src)
            outjson = os.path.join(dst, os.path.splitext(rel)[0] + ".json")
            os.makedirs(os.path.dirname(outjson), exist_ok=True)
            try:
                subprocess.run([CONVERTER, sk, outjson], check=True, capture_output=True)
                with open(outjson, encoding="utf-8") as f:
                    data = json.load(f)
                with open(outjson, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                ok += 1
                print(f"OK:   {rel}")
                loglines.append(f"OK:   {rel}")
            except Exception as e:
                failed += 1
                print(f"FAIL: {rel}: {e}")
                loglines.append(f"FAIL: {rel}: {e}")

        for root, _, files in os.walk(src):
            for f in files:
                if os.path.splitext(f)[1].lower() in IMG_EXTS:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, src)
                    outpath = os.path.join(dst, rel)
                    os.makedirs(os.path.dirname(outpath), exist_ok=True)
                    shutil.copy2(full, outpath)

        loglines.append(f"done: {ok} ok, {failed} failed")
        with open(os.path.join(dst, "convert-log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(loglines) + "\n")

        with zipfile.ZipFile(zout, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(dst):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, dst)
                    z.write(full, rel)

        print(f"done: {ok} ok, {failed} failed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()