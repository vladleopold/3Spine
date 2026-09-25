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

        has_editor = bool(shutil.which(spine)) or (os.path.exists(spine) and os.access(spine, os.X_OK))
        if not has_editor:
            say(f"compile-block: WARN Spine Editor не найден (SPINE_EDITOR={spine}) — "
                f"файлов .spine не создано, остаются JSON/.skel указанной версии")
        else:
            say(f"compile-block: Spine Editor: {spine}")

            # ── ЭТАП 1: .skel без пары .json → экспорт в .json редактором ──────────
            skels = []
            for root, _, files in os.walk(src):
                for f in sorted(files):
                    if not f.lower().endswith(".skel"):
                        continue
                    p = os.path.join(root, f)
                    base = os.path.splitext(p)[0]
                    if not os.path.exists(base + ".json"):
                        skels.append((p, base + ".json", os.path.relpath(p, src)))
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
                    attempts = ([base_cmd() + ["-u", ver] + tail] if (ver and not fallback) else []) \
                        + [base_cmd() + tail] * 3
                    rc = -1
                    for idx, cmd in enumerate(attempts):
                        used = ("версия " + ver) if (ver and idx == 0 and not fallback) else "последняя"
                        rc = run(cmd)
                        if os.path.exists(out_spine) and os.path.getsize(out_spine) > 0:
                            return rel, True, f"compile-block: ✓ {rel} → {os.path.basename(out_spine)} (Spine {ver}, {used})"
                        if idx < len(attempts) - 1:
                            time.sleep(1.5 + idx)
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

        with zipfile.ZipFile(zout, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(src):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, src))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
