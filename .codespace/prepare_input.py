#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Хэш-анализатор + блок переименования (hash → asset_name).
# Ставится МЕЖДУ загрузкой/авторизацией пользователя и блоком-конвертации:
#   1) анализирует имена файлов во входном zip (рекурсивно, все форматы);
#   2) если найдены SHA-256 хеш-имена → запускает блок переименования
#      (hash_to_name_sha256_algorithm → restore_names_cli.py), который
#      восстанавливает оригинальные asset_name ДЛЯ ВСЕХ форматов в подпапках;
#   3) если хеш-имён нет → первый путь: без изменений, сразу конвертация.
# Использование: python3 prepare_input.py <input.zip> <output.zip>
import os
import re
import sys
import shutil
import tempfile
import zipfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "hash_to_name"))

from hash_to_name import is_hash_name                     # SHA-256 детектор  # noqa: E402
from restore_names_cli import process_folder as restore_names  # hash → asset_name  # noqa: E402
from manifest_resolver import apply_manifest_names       # hash → имя по манифестам игры  # noqa: E402


def unzip(zin_path: str, dst: str) -> None:
    with zipfile.ZipFile(zin_path) as z:
        for name in z.namelist():
            target = os.path.join(dst, name)
            if name.endswith("/"):
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(name) as f, open(target, "wb") as o:
                    shutil.copyfileobj(f, o)


def rezip(src: str, zout_path: str) -> None:
    with zipfile.ZipFile(zout_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(src):
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, src))


def find_hashed_files(src: str) -> list[str]:
    found = []
    for root, _, files in os.walk(src):
        for f in sorted(files):
            if is_hash_name(f):
                found.append(os.path.join(root, f))
    return found


def is_binary_spine(path: str) -> bool:
    """True, если файл — бинарный Spine-скелет (не валидный JSON по содержанию).
    Формат .skel: [len][hash][len][version: N.N.N(M.M)][body...]."""
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


def fix_misnamed_json(src: str, loglines: list[str]) -> int:
    """Спецслучай: .json, внутри которого на самом деле бинарный .skel
    (юзер программно переименовал skel→json) → возвращаем расширение .skel.
    Дальше блок-конвертации сделает из него настоящий JSON для Spine."""
    fixed = 0
    for root, _, files in os.walk(src):
        for f in sorted(files):
            if not f.lower().endswith(".json"):
                continue
            p = os.path.join(root, f)
            if not is_binary_spine(p):
                continue
            base = os.path.splitext(f)[0]
            target = os.path.join(root, base + ".skel")
            i = 2
            while os.path.exists(target):
                target = os.path.join(root, f"{base}_{i}.skel")
                i += 1
            os.rename(p, target)
            fixed += 1
            msg = f"magic-detect: {f} → {os.path.basename(target)} (бинарь skel под видом json)"
            print(msg)
            loglines.append(msg)
    return fixed


def main() -> None:
    zin, zout = sys.argv[1], sys.argv[2]
    tmp = tempfile.mkdtemp()
    loglines = []
    try:
        src = os.path.join(tmp, "in")
        os.makedirs(src)
        unzip(zin, src)

        hashed = find_hashed_files(src)
        renamed = 0
        if hashed:
            print(f"hash-analyzer: найдено {len(hashed)} файлов с SHA-256 хеш-именами "
                  f"→ запускаю блок переименования (hash → asset_name)")
            loglines.append(f"hash-analyzer: {len(hashed)} файлов с хеш-именами")

            # 1) манифесты игры: единственный источник, который знает ВСЕ имена
            by_manifest, m_details = apply_manifest_names(src, dry_run=False)
            if by_manifest:
                loglines.append(f"hash-to-name(манифест): переименовано {by_manifest} файлов")
                print(f"hash-to-name(манифест): переименовано {by_manifest} файлов")
                for line in m_details[:20]:
                    print("  " + line)
                if len(m_details) > 20:
                    print(f"  … и ещё {len(m_details) - 20}")

            # 2) остаток — эвристики атласов/xml/json (restore_names_cli)
            left = find_hashed_files(src)
            if left:
                print(f"hash-to-name: осталось {len(left)} хеш-имён → эвристики атласов/ссылок")
                renamed = restore_names(Path(src), dry_run=False, recursive=True)
                loglines.append(f"hash-to-name(эвристики): переименовано {renamed}, "
                                f"осталось хеш-имён: {len(find_hashed_files(src))}")
                print(f"hash-to-name: итого переименовано эвристиками {renamed} файлов")
            else:
                loglines.append("hash-to-name: все хеш-имена разрешены через манифест")
        else:
            print("hash-analyzer: хеш-имён не найдено → первый путь (без переименования)")
            loglines.append("hash-analyzer: хеш-имён нет — путь без переименования")

        fixed_json = fix_misnamed_json(src, loglines)
        if fixed_json:
            loglines.append(f"magic-detect: всего исправлено расширений: {fixed_json}")

        with open(os.path.join(src, "prepare-log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(loglines) + "\n")

        rezip(src, zout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()