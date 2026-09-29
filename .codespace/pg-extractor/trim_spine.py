#!/usr/bin/env python3
"""Ужимает pg_out/spine под лимит GitHub, не выбрасывая карточки целиком.

Раньше при превышении лимита удалялся самый тяжёлый проект — карточка
исчезала совсем. Теперь сначала у тяжёлых проектов снимаются страницы,
добавленные из общего пула текстур (<имя>xN.png, их ставит
extract_pragmatic_uht.py): карточка остаётся, но рисует меньше картинок.
И только когда это не помогает, проект удаляется целиком.

    python3 trim_spine.py pg_out/spine 85
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def page_blocks(lines: list[str]) -> list[tuple[str, int, int]]:
    """Границы страниц в .atlas: (имя страницы, начало, конец)."""
    starts = [
        i for i in range(len(lines) - 4)
        if lines[i] and not lines[i][0].isspace()
        and lines[i + 1].strip().startswith("size:")
        and lines[i + 2].strip().startswith("format:")
        and lines[i + 3].strip().startswith("filter:")
        and lines[i + 4].strip().startswith("repeat:")
    ]
    out = []
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        out.append((lines[start].strip(), start, end))
    return out


def strip_pool_pages(proj: Path) -> int:
    """Убирает страницы-пул из проекта. Возвращает освобождённые байты."""
    name = proj.name
    pool = sorted(p for p in proj.glob(f"{name}x*.png") if p.stat().st_size)
    atlas = proj / f"{name}.atlas"
    if not pool or not atlas.exists():
        return 0
    lines = atlas.read_text(encoding="utf-8").splitlines()
    drop = {p.name for p in pool}
    kept = [b for b in page_blocks(lines) if b[0] not in drop]
    if len(kept) == len(page_blocks(lines)):
        return 0
    if not kept:
        return 0                       # единственная страница — проект и так нужен
    head = [l for l in lines[:page_blocks(lines)[0][1]] if l.strip()]
    body: list[str] = []
    for _p, start, end in kept:
        body.extend(lines[start:end])
    atlas.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    freed = sum(p.stat().st_size for p in pool)
    for p in pool:
        p.unlink()
    return freed


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: trim_spine.py <dir> <cap_mb>", file=sys.stderr)
        return 2
    root = Path(argv[1])
    cap = int(argv[2]) * 1024 * 1024
    if not root.is_dir():
        return 0

    def total() -> int:
        return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())

    while total() > cap:
        projects = [p for p in root.iterdir() if p.is_dir()]
        if not projects:
            break
        freed_any = False
        for proj in sorted(projects, key=dir_size, reverse=True):
            freed = strip_pool_pages(proj)
            if freed:
                freed_any = True
                print(f"::warning::{proj.name}: снял {freed // 1048576} МБ страниц из общего "
                      f"пула (карточка останется, но часть картинок пропадёт)")
                if total() <= cap:
                    break
        if freed_any:
            continue
        big = max(projects, key=dir_size)
        print(f"::warning::подрезаю {big.name} — общий размер выше {cap // 1048576} МБ")
        shutil.rmtree(big)

    print(f"Итог после подрезки: {total() // 1048576} МБ, "
          f"проектов {len([p for p in root.iterdir() if p.is_dir()])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
