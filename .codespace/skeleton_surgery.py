#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Хирургическое восстановление секции костей/слотов.
#
# Отличие от общего «лечения»: здесь задан проверяемый критерий результата
# (число костей + читаемость имён), поэтому перебор значений ведётся не по
# «прогрессу разбора», а по критерию правдоподобия. Каждому потерянному байту
# (U+FFFD) подставляется значение из списка правдоподобных байтов float32:
# нули, 0x3F/0x40 (экспонента 1.0/2.0), 0x80/0xBF/0xC0 (мантисса), 0x7F, 0xFF…
#
# Использование:
#   python3 skeleton_surgery.py reels_fx.skel --bones 11 -o fixed/
#   python3 skeleton_surgery.py reels_fx.skel --bones 11 --time-limit 120
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if os.path.join(HERE, "spine_restore") not in sys.path:
    sys.path.insert(0, os.path.join(HERE, "spine_restore"))

import skeleton_repair as R  # noqa: E402

FFFD = b"\xef\xbf\xbd"

# Наиболее частые байты float32/varint в скелетах Spine.
# 0x00 — ноль, 0x3F — 0.7..1.0, 0x40 — 2.0, 0x80/0xBF — мантисса,
# 0xC0 — -2.0, 0x7F/0xFF — пределы, 0x01 — мелкие счётчики.
CANDIDATES = [0x00, 0x3F, 0x40, 0x80, 0xBF, 0xC0, 0x01, 0x7F, 0xFF, 0x20, 0x10]


# ── раскрытие U+FFFD по паттернам float32 ────────────────────────────────
# 3F EF BF BD 00 00  →  3F 80 00 00   = 1.0f   (FFFD скрыл мантиссу)
# C0 EF BF BD EF BF BD → C0 00 00 00 = -2.0f
# 00 EF BF BD        →  00           (обычный байт)
EXP_HI = set(range(0x3B, 0x42)) | {0xC0, 0xC1, 0xC2, 0x38, 0x39, 0x3A, 0x3C, 0xBD, 0xBE}


def smart_expand(data: bytes, fill: int = 0x00, mantissa: bool = True) -> bytes:
    """Разворачивает каждую EF BF BD в исходные байты.

    Если перед группой стоит байт экспоненты float32 (3B..41, C0..C2), то группа
    закрывала старшие байты мантиссы → восстанавливаем 80 00 00 (значения вида
    1.0 / 2.0 / -2.0). Иначе группа — одиночный байт → fill (0x00 или 0x80).
    """
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        if data.startswith(FFFD, i):
            prev = out[-1] if out else 0
            nxt = data[i + 3] if i + 3 < n else None
            wide = (mantissa and prev in EXP_HI
                    and (nxt is None or nxt == 0x00 or nxt in EXP_HI or 0x30 <= nxt <= 0x39))
            if wide:
                out += b"\x80\x00\x00"
            else:
                out.append(fill)
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def check(expanded: bytes, target_bones: int = 0):
    """Разбирает раскрытые данные обычным парсером и оценивает результат."""
    reader = R.Spine38BinaryReader(expanded)
    doc = None
    try:
        doc = reader.read_skeleton_data()
    except Exception as e:               # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    return doc, ""


def name_ok(name: str, max_len: int = 40) -> bool:
    """Имя пригодно, если это печатный ASCII без управляющих символов."""
    if not name or len(name) > max_len:
        return False
    return all(32 <= ord(ch) < 127 for ch in name)


def evaluate(reader_cls, units, decisions):
    """Разбор с заданными правками. Возвращает (документ, позиция, ok)."""
    r = reader_cls(units, decisions)
    r.best_effort = True
    r.eof_zero = True
    doc = None
    try:
        doc = r.read_skeleton_data()
    except Exception:                    # noqa: BLE001
        doc = None
    return doc, r.upos


def score(doc, target_bones: int) -> int:
    """Оценка правдоподобия: целевое число костей — главный критерий."""
    if not doc:
        return -1
    bones = doc.get("bones") or []
    slots = doc.get("slots") or []
    sc = 0
    if target_bones and len(bones) == target_bones:
        sc += 1000
    sc += 10 * sum(1 for b in bones if name_ok(b.get("name", "")))
    sc += 5 * sum(1 for s in slots if name_ok(s.get("name", "")))
    sc += 3 * sum(1 for s in slots if s.get("bone") in {b.get("name") for b in bones})
    sc += min(len(doc.get("animations") or {}), 12)
    if bones and not name_ok(bones[0].get("name", "")):
        sc -= 500
    return sc


def surgery(data: bytes, target_bones: int = 0, time_limit: float = 100.0,
            limit_units: int = 30000, max_decisions: int = 12, groups: int = 6):
    """Перебор комбинаций «ширина + значение» для первых повреждённых байт.

    Оракул — проверяемый критерий: целевое число костей и читаемость имён.
    Отбор состояний идёт по позиции разбора (быстрый прокси), принятие — по
    критерию. Возвращает (документ|None, правки, инфо).
    """
    import time as _t
    t0 = _t.monotonic()
    units = R.scan_units(data)
    cls = R.make_soft_reader_class()
    unks = [i for i, u in enumerate(units) if isinstance(u, tuple) and i < limit_units][:groups]
    if not unks:
        return None, None, {"error": "нет повреждений в начале файла"}
    values = CANDIDATES[:6]
    options = [(1, 0)] + [(w, v) for w in (2, 3, 4) for v in values]

    def run(dec):
        r = cls(units, dec)
        r.best_effort = True
        r.eof_zero = True
        doc = None
        try:
            doc = r.read_skeleton_data()
        except Exception:                # noqa: BLE001
            doc = None
        return doc, r.upos

    doc0, pos0 = run({})
    best = (score(doc0, target_bones), {}, pos0, doc0)
    tried = 0
    # стек состояний: (индекс группы, правки, позиция)
    stack = [(0, {}, pos0)]
    visited = set()
    while stack and _t.monotonic() - t0 < time_limit:
        gi, dec, pos = stack.pop()
        if gi >= len(unks):
            continue
        key = (gi, tuple(sorted((k, tuple(v)) for k, v in dec.items())))
        if key in visited:
            continue
        visited.add(key)
        unit = unks[gi]
        children = []
        for opt in options:
            if _t.monotonic() - t0 > time_limit:
                break
            nd = dict(dec)
            if opt == (1, 0):
                nd.pop(unit, None)
            else:
                nd[unit] = opt
            doc, npos = run(nd)
            tried += 1
            if npos < pos0 and len(nd) == 0:
                continue
            sc = score(doc, target_bones)
            if sc > best[0]:
                best = (sc, dict(nd), npos, doc)
            if npos >= pos:
                children.append((npos, gi + 1, nd))
        # сначала исследуем самые перспективные (по позиции) ветви
        children.sort(key=lambda x: -x[0])
        for npos, ngi, nd in children:
            stack.append((ngi, nd, npos))
        if len(stack) > 20000:
            stack = stack[:10000]
    return best[3], best[1], {"tries": tried, "score": best[0],
                              "seconds": round(_t.monotonic() - t0, 1)}


OPTIONS = [
    ("00", 1, 0x00),
    ("80", 1, 0x80),
    ("1.0", 3, [0x80, 0x00, 0x00]),
    ("1.0b", 4, [0x00, 0x80, 0x00, 0x00]),
    ("-2.0", 4, [0x00, 0x00, 0x00, 0xC0]),
    ("0.5", 3, [0x00, 0x00, 0x3F]),
]


def options_for(unit_prev: int, unit_next: int):
    """Набор правок для одного FFFD: одиночный байт либо хвост float32."""
    out = [(t, 1, v) for t, _w, v in OPTIONS if _w == 1]
    if unit_prev in R and False:            # заглушка, реальные байты не нужны
        pass
    return out


def surgery(data: bytes, target_bones: int = 0, groups: int = 8, time_limit: float = 100.0):
    """Перебор комбинаций раскрытия первых повреждённых байт по критерию."""
    import itertools
    import time as _t
    t0 = _t.monotonic()
    units = R.scan_units(data)
    cls = R.make_soft_reader_class()
    unks = [i for i, u in enumerate(units) if isinstance(u, tuple)][:groups]
    if not unks:
        return None, None, {"error": "нет повреждений"}
    variants = [("1", 1, 0x00), ("1", 1, 0x80), ("3", 3, [0x80, 0x00, 0x00]),
                ("4", 4, [0x00, 0x80, 0x00, 0x00]), ("2", 2, [0x80, 0x00])]
    best = (-1, None, None)
    tried = 0
    for combo in itertools.product(variants, repeat=len(unks)):
        if _t.monotonic() - t0 > time_limit:
            break
        dec = {u: (v[0] if v[0] == 1 else v[1], v[2]) for u, v in zip(unks, combo)}
        r = cls(units, dec)
        r.best_effort = True
        r.eof_zero = True
        doc = None
        try:
            doc = r.read_skeleton_data()
        except Exception:                    # noqa: BLE001
            doc = None
        tried += 1
        if not doc:
            continue
        sc = score(doc, target_bones)
        if sc > best[0]:
            best = (sc, doc, {str(k): v for k, v in dec.items()})
            b = doc.get("bones") or []
            good = sum(1 for x in b if name_ok(x.get("name", "")))
            print(f"    костей {len(b)} (имён {good}), слотов {len(doc.get('slots') or [])}, "
                  f"анимаций {len(doc.get('animations') or {})} → score {sc}")
    # отдельный проход: подбор значения счётчика костей (он задаёт рамку разбора)
    if best[1] is not None:
        pos = _count_position(cls, units, best[2])
        if pos is not None:
            for v in range(0, 160):
                if _t.monotonic() - t0 > time_limit:
                    break
                dec = dict(best[2])
                dec[str(pos)] = (1, v)
                r = cls(units, dec)
                r.best_effort = True
                r.eof_zero = True
                doc = None
                try:
                    doc = r.read_skeleton_data()
                except Exception:            # noqa: BLE001
                    doc = None
                tried += 1
                if not doc:
                    continue
                sc = score(doc, target_bones)
                if sc > best[0]:
                    best = (sc, doc, dec)
                    b = doc.get("bones") or []
                    good = sum(1 for x in b if name_ok(x.get("name", "")))
                    print(f"    счётчик={v}: костей {len(b)} (имён {good}), "
                          f"слотов {len(doc.get('slots') or [])}, "
                          f"анимаций {len(doc.get('animations') or {})} → score {sc}")
    return best[1], best[2], {"tries": tried, "score": best[0],
                              "seconds": round(_t.monotonic() - t0, 1)}


def _count_position(cls, units, decisions):
    """Позиция юнита, из которого читается число костей (третий varint)."""
    reads = []

    class Probe(cls):
        def read_int_var(self, optimize_positive=True):
            before = self.upos
            v = super().read_int_var(optimize_positive)
            if len(reads) < 3:
                reads.append(before)
            return v

    dec = {int(k): v for k, v in (decisions or {}).items()}
    r = Probe(units, dec)
    r.best_effort = True
    r.eof_zero = True
    try:
        r.read_skeleton_data()
    except Exception:                        # noqa: BLE001
        pass
    return reads[2] if len(reads) > 2 else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Хирургия секции костей/слотов")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--bones", type=int, default=0)
    ap.add_argument("--groups", type=int, default=8)
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--time-limit", type=float, default=100.0)
    args = ap.parse_args()

    for path in args.files:
        with open(path, "rb") as f:
            data = f.read()
        print(f"{os.path.basename(path)}: {len(data)} байт, повреждений {data.count(FFFD)}")
        doc, dec, info = surgery(data, args.bones, args.groups, args.time_limit)
        if not doc:
            print("  не восстановлено:", info)
            continue
        bones = doc.get("bones") or []
        good = sum(1 for b in bones if name_ok(b.get("name", "")))
        print(f"  ГОТОВО: костей {len(bones)} (читаемых имён {good}), "
              f"слотов {len(doc.get('slots') or [])}, "
              f"анимаций {list((doc.get('animations') or {}).keys())[:8]}, {info}")
        print("  правки:", dec)
        # сохраняем только если критерий выполнен: все имена читаемы и
        # (если задан) число костей совпало — иначе это мусор, а не «ремонт»
        bones = doc.get("bones") or []
        all_names_ok = bool(bones) and all(name_ok(b.get("name", "")) for b in bones)
        count_ok = (not args.bones) or len(bones) == args.bones
        if not (all_names_ok and count_ok):
            print(f"  критерий не выполнен: костей {len(bones)}, "
                  f"все имена читаемы: {all_names_ok}, число костей совпало: {count_ok} — не сохраняю")
            continue
        if args.out:
            os.makedirs(args.out, exist_ok=True)
            stem = os.path.splitext(os.path.basename(path))[0]
            out_json = os.path.join(args.out, stem + ".fixed.json")
            with open(out_json, "w", encoding="utf-8") as f:
                json.dump(R.sanitize(doc), f, ensure_ascii=False, indent=1)
            print("  сохранено:", out_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
