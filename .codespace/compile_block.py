#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Блок-Компиляции: конвертированный Spine-JSON → файл редактора .spine
# той версии, что указана в самом JSON (редактор сам подбирает нужный рантайм).
# Нужен лицензированный Spine Editor: env SPINE_EDITOR или "Spine" в PATH.
# Файлы складываются в ТУ ЖЕ структуру папок, что дал юзер.
# Если редактор недоступен — WARN в compile-log.txt, остаются JSON/.skel версии.
# Использование: compile_block.py <input.zip> <output.zip>
import os
import sys
import json
import shutil
import zipfile
import subprocess
import tempfile


def main() -> None:
    zin, zout = sys.argv[1], sys.argv[2]
    spine = os.environ.get("SPINE_EDITOR") or "Spine"
    tmp = tempfile.mkdtemp()
    loglines: list[str] = []
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

        compiled = skipped = failed = 0
        has_editor = bool(shutil.which(spine)) or (os.path.exists(spine) and os.access(spine, os.X_OK))
        if not has_editor:
            msg = (f"compile-block: WARN Spine Editor не найден (SPINE_EDITOR={spine}) — "
                   f"файлов .spine не создано, остаются JSON/.skel указанной версии")
            print(msg)
            loglines.append(msg)
        else:
            loglines.append(f"compile-block: Spine Editor: {spine}")
            for root, _, files in os.walk(src):
                for f in sorted(files):
                    if not f.lower().endswith(".json"):
                        continue
                    p = os.path.join(root, f)
                    d = None
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
                        continue  # не скелет
                    out_spine = os.path.join(root, os.path.splitext(f)[0] + ".spine")
                    os.makedirs(os.path.dirname(out_spine), exist_ok=True)
                    try:
                        r = subprocess.run([spine, "-i", p, "-o", out_spine, "-r"],
                                           capture_output=True, text=True, timeout=600)
                        if os.path.exists(out_spine) and os.path.getsize(out_spine) > 0:
                            compiled += 1
                            m = f"compile-block: ✓ {f} → {os.path.basename(out_spine)} (Spine {ver})"
                            print(m)
                            loglines.append(m)
                        else:
                            failed += 1
                            m = f"compile-block: FAIL {f}: rc={r.returncode} {r.stderr[-300:]}"
                            print(m)
                            loglines.append(m)
                    except Exception as e:
                        failed += 1
                        m = f"compile-block: FAIL {f}: {e}"
                        print(m)
                        loglines.append(m)

            loglines.append(f"compile-block: итого скомпилировано {compiled}, пропущено {skipped}, ошибок {failed}")

        with open(os.path.join(src, "compile-log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(loglines) + "\n")

        with zipfile.ZipFile(zout, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(src):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, src))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()