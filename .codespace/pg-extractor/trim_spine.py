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

import json
import shutil
import sys
from pathlib import Path

# Пороги «проект-гигант» для снятия страниц общего пула. Мелкие проекты не
# трогаем вовсе: у них собственные страницы копеечные, и снятие пула ломает
# карточку сильнее, чем экономит.
MIN_STRIP_BYTES = 8 * 1024 * 1024
MAX_POOL_SHARE = 0.75

# Снять пул можно только если карточка от этого не потеряет ни одной картинки.
# Раньше это правило было «пул — меньшинство веса проекта», и оно срабатывало
# на проектах, у которых ВЕСЬ рисунок как раз в пуле: снимали 2 страницы —
# карточка теряла все 20 регионов (wran_gamble_screen_in_fx). Теперь сверяемся
# с тем, что скелет реально просит.
try:                                    # переиспользуем разбор регионов экстрактора
    from extract_pragmatic_uht import atlas_region_name, spine_image_refs
except Exception:                       # pragma: no cover - страховка, не молча портим
    atlas_region_name = spine_image_refs = None


def needed_regions(proj: Path, name: str) -> set[str]:
    """Имена регионов, которые скелет проекта реально использует."""
    if spine_image_refs is None:
        return set()
    skel = proj / f"{name}.json"
    try:
        return set(spine_image_refs(json.loads(skel.read_text(encoding="utf-8"))))
    except Exception:
        return set()


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
    """Убирает страницы-пул из проекта. Возвращает освобождённые байты.

    Страницы-пул снимаются только у ПРОЕКТОВ-ГИГАНТОВ, у которых собственные
    страницы и так покрывают карточку. Раньше правило было «самый тяжёлый
    проект», и под нож попадали как раз мелкие проекты, у которых собственные
    страницы копеечные, а все картинки пришли из общего пула: сняли пул — и
    карточка потеряла половину графики (а если пул был весь — становилась
    пустой заглушкой). Теперь порог: проект крупнее MIN_STRIP_MB и пул в нём —
    меньшинство (меньше MAX_POOL_SHARE от его веса).
    """
    name = proj.name
    pool = sorted(p for p in proj.glob(f"{name}x*.png") if p.stat().st_size)
    atlas = proj / f"{name}.atlas"
    if not pool or not atlas.exists():
        return 0
    freed_pool = sum(p.stat().st_size for p in pool)
    own = dir_size(proj) - freed_pool
    if dir_size(proj) < MIN_STRIP_BYTES:
        return 0                      # мелкий проект: жалко его не портить
    if freed_pool > own * MAX_POOL_SHARE:
        return 0                      # пул here и есть вся графика — не трогаем
    lines = atlas.read_text(encoding="utf-8").splitlines()
    blocks = page_blocks(lines)
    drop = {p.name for p in pool}
    kept = [b for b in blocks if b[0] not in drop]
    if len(kept) == len(blocks):
        return 0
    if not kept:
        return 0                       # единственная страница — проект и так нужен
    # Главное правило: если хоть один нужный скелету регион лежит на странице
    # пула — пул несущий, снимать его нельзя. Иначе карточка теряет картинки
    # («не хватает N картинок в атласе»), и это хуже, чем лишние мегабайты.
    need = needed_regions(proj, name)
    if need:
        pool_regions: set[str] = set()
        for pn, _s, _e, _r in blocks:
            if pn in drop:
                pool_regions.update(
                    atlas_region_name(l.strip()) for l in lines[_s + 5:_e]
                    if l and not l[0].isspace()
                )
        load_bearing = need & pool_regions
        if load_bearing:
            return 0
    head = [l for l in lines[:blocks[0][1]] if l.strip()]
    body: list[str] = []
    for _p, start, end in kept:
        body.extend(lines[start:end])
    atlas.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    for p in pool:
        p.unlink()
    return freed_pool


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
