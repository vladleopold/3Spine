#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
hash_to_name.py

Преобразование имени файла:
    SHA256(байты файла).lowercase_hex + исходное расширение

Пример:
    original.png
    ->
    d0f4d02b684445104daf001fe270b42378f31e203f98f45b194afe4d300829df.png

SHA-256 вычисляется от содержимого файла, а не от его исходного имени.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from pathlib import Path

CHUNK_SIZE = 1024 * 1024

# None = обрабатывать любые файлы.
SUPPORTED_EXTENSIONS = {
    ".png", ".svg", ".xml", ".json", ".atlas",
    ".jpg", ".jpeg", ".webp", ".gif",
    ".mp3", ".wav", ".ogg", ".bin",
}

HASH_NAME_RE = re.compile(r"^[0-9a-fA-F]{64}(?:\.[^.]*)?$")


def sha256_file(path: Path) -> str:
    """Вычислить SHA-256 полного содержимого файла."""
    digest = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)

    return digest.hexdigest()


def get_hash_name(path: Path) -> str:
    """Получить имя вида <sha256>.<исходное расширение>."""
    return sha256_file(path) + path.suffix.lower()


def get_hash_path(path: Path) -> Path:
    """Получить новый полный путь, не переименовывая файл."""
    return path.with_name(get_hash_name(path))


def is_hash_name(filename: str) -> bool:
    """Проверить, является ли имя SHA-256 именем."""
    return HASH_NAME_RE.fullmatch(filename) is not None


def verify_hash_name(path: Path) -> bool:
    """Проверить, соответствует ли hash в имени содержимому файла."""
    if not is_hash_name(path.name):
        return False

    expected = path.stem.lower()
    if len(expected) != 64:
        return False

    return sha256_file(path) == expected


def rename_to_hash(path: Path, dry_run: bool = False) -> Path:
    """Переименовать один файл в SHA-256 имя."""
    if not path.is_file():
        raise FileNotFoundError(path)

    new_path = path.with_name(get_hash_name(path))

    if path == new_path:
        return path

    if new_path.exists():
        # Если это идентичный файл, считаем операцию уже выполненной.
        if new_path.is_file() and sha256_file(new_path) == sha256_file(path):
            return new_path
        raise FileExistsError(f"Целевой файл уже существует: {new_path}")

    if dry_run:
        print(f"{path.name} -> {new_path.name}")
        return new_path

    path.rename(new_path)
    return new_path


def rename_directory(
    directory: Path,
    recursive: bool = False,
    dry_run: bool = False,
) -> list[tuple[Path, Path]]:
    """Переименовать подходящие файлы в директории."""
    if not directory.is_dir():
        raise NotADirectoryError(directory)

    files = directory.rglob("*") if recursive else directory.iterdir()
    results = []

    for path in files:
        if not path.is_file():
            continue

        if (
            SUPPORTED_EXTENSIONS is not None
            and path.suffix.lower() not in SUPPORTED_EXTENSIONS
        ):
            continue

        if is_hash_name(path.name):
            if verify_hash_name(path):
                print(f"[OK] {path}")
            else:
                print(f"[WARN] hash имени не соответствует содержимому: {path}")
            continue

        try:
            new_path = rename_to_hash(path, dry_run=dry_run)
            if new_path != path and (dry_run or path.exists() is False):
                results.append((path, new_path))
                print(f"[HASH] {path.name}")
                print(f"       -> {new_path.name}")
        except Exception as exc:
            print(f"[ERROR] {path}: {exc}")

    return results


# ----------------------------------------------------------------------
# API для интеграции в существующую программу
# ----------------------------------------------------------------------

def hash_to_name(file_path: str | os.PathLike) -> str:
    """
    Главная функция:
        /path/file.png
        ->
        <sha256>.png
    """
    return get_hash_name(Path(file_path))


def convert_path_to_hash_name(file_path: str | os.PathLike) -> str:
    """
    Вернуть полный путь с новым hash-именем.
    Исходный файл не изменяется.
    """
    path = Path(file_path)
    return str(path.with_name(get_hash_name(path)))


def rename_file_to_hash(file_path: str | os.PathLike) -> str:
    """Реально переименовать файл и вернуть новый полный путь."""
    return str(rename_to_hash(Path(file_path)))


def process_path(
    path: str | os.PathLike,
    recursive: bool = False,
    dry_run: bool = False,
):
    """Универсальная точка входа для файла или директории."""
    target = Path(path)

    if target.is_file():
        return rename_to_hash(target, dry_run=dry_run)

    if target.is_dir():
        return rename_directory(
            target,
            recursive=recursive,
            dry_run=dry_run,
        )

    raise FileNotFoundError(target)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename files using SHA-256 of file contents."
    )
    parser.add_argument("path", help="Файл или директория")
    parser.add_argument(
        "--recursive", "-r",
        action="store_true",
        help="Обрабатывать вложенные директории",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только показать новые имена, не переименовывать",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Проверить SHA-256 имя существующего файла",
    )

    args = parser.parse_args()
    target = Path(args.path)

    if args.verify:
        if not target.is_file():
            raise SystemExit("--verify требует путь к файлу")
        print("OK" if verify_hash_name(target) else "FAIL")
        return

    result = process_path(
        target,
        recursive=args.recursive,
        dry_run=args.dry_run,
    )

    if target.is_file():
        print(f"\nРезультат: {result}")


if __name__ == "__main__":
    main()
