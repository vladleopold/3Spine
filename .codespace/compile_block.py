#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Блок-Компиляции: конвертированные Spine-JSON → файлы редактора .spine
# той версии, что указана в самом JSON. Компиляция идёт ПАРАЛЛЕЛЬНО: по одному
# процессу Spine на скелет, до SPINE_WORKERS одновременно.
# Нужен лицензированный Spine Editor: env SPINE_EDITOR или "Spine" в PATH.
# Лицензия активируется один раз перед стартом (env SPINE_LICENSE, stdin).
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
import time
from concurrent.futures import ThreadPoolExecutor


def editor_workers(count: int) -> int:
    raw = os.environ.get("SPINE_WORKERS", "0") or "0"
    n = int(raw) if raw.isdigit() else 0
    if n <= 0:
        n = min(4, max(1, os.cpu_count() or 1))
    return max(1, min(n, max(1, count)))


def main() -> None:
    zin, zout = sys.argv[1], sys.argv[2]
    spine = os.environ.get("SPINE_EDITOR") or "Spine"
    license_code = os.environ.get("SPINE_LICENSE", "")
    xmx = os.environ.get("SPINE_XMX", "1024") if editor_workers(999) > 1 else ""
    tmp = tempfile.mkdtemp()
    loglines: list[str] = []
    started = time.time()
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

        has_editor = bool(shutil.which(spine)) or (os.path.exists(spine) and os.access(spine, os.X_OK))
        if not has_editor:
            msg = (f"compile-block: WARN Spine Editor не найден (SPINE_EDITOR={spine}) — "
                   f"файлов .spine не создано, остаются JSON/.skel указанной версии")
            print(msg)
            loglines.append(msg)
        else:
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
                    out_spine = os.path.join(root, os.path.splitext(f)[0] + ".spine")
                    jobs.append((p, out_spine, ver, os.path.relpath(p, src)))

            jobs.sort(key=lambda j: j[3])
            workers = editor_workers(len(jobs))
            loglines.append(f"compile-block: Spine Editor: {spine}")
            loglines.append(f"compile-block: скелетов {len(jobs)}, параллельно {workers}"
                            + (f", -Xmx{xmx}m" if xmx else ""))
            if license_code:
                loglines.append("compile-block: активация лицензии (SPINE_LICENSE задан)")

            stdin = (license_code + "\n") if license_code else None
            results = []

            def run_one(job, fallback=False):
                p, out_spine, ver, rel = job
                base = [spine] + (["-Xmx" + xmx + "m"] if xmx else [])
                tail = ["-i", p, "-o", out_spine, "-r"]
                if fallback or not ver:
                    attempts = [base + tail] * 4
                else:
                    attempts = [base + ["-u", ver] + tail] + [base + tail] * 3
                last = ""
                for idx, cmd in enumerate(attempts):
                    used = "версия " + ver if (ver and idx == 0 and not fallback) else "последняя"
                    try:
                        r = subprocess.run(cmd, input=stdin, capture_output=False,
                                           text=True, timeout=1800)
                        rc = r.returncode
                    except Exception as e:
                        rc, last = -1, str(e)
                    if os.path.exists(out_spine) and os.path.getsize(out_spine) > 0:
                        return rel, True, f"compile-block: ✓ {rel} → {os.path.basename(out_spine)} (Spine {ver}, {used})"
                    if idx == len(attempts) - 1:
                        return rel, False, f"compile-block: FAIL {rel}: rc={rc} {last}".strip()
                    time.sleep(1.5 + idx)

            if not jobs:
                pass
            else:
                warm = run_one(jobs[0])
                results.append(warm)
                rest = jobs[1:]
                if workers == 1 or not rest:
                    results.extend(run_one(j) for j in rest)
                else:
                    with ThreadPoolExecutor(max_workers=workers - 1 if len(results) else workers) as pool:
                        results.extend(pool.map(lambda j: run_one(j, fallback=True), rest))

            compiled = failed = 0
            for _rel, ok, msg in results:
                if ok:
                    compiled += 1
                else:
                    failed += 1
                print(msg)
                loglines.append(msg)

            loglines.append(f"compile-block: итого скомпилировано {compiled}, ошибок {failed}, "
                            f"время {time.time() - started:.1f}s")

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
