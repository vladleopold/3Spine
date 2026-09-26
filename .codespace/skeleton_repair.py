#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Лечение Spine-скелетов, повреждённых заменой байтов на U+FFFD.
#
# Что происходит с файлом: байты >= 0x80, которые не являются началом
# корректной UTF-8 последовательности, при перекодировке в UTF-8 с
# errors="replace" превращаются в 3 байта EF BF BD. Длина файла растёт на
# 2 байта на каждый такой байт, сами значения теряются.
#
# Что можно восстановить:
#   * структуру (счётчики, длины, типы) — она often не затронута, потому что
#     счётчики малы и кодируются одним байтом < 0x80;
#   * внутри float32 — подбираем байты так, чтобы значение осталось правдоподобным;
#   * внутри строк — ставим '?';
#   * потерянный старший байт varint (счётчик/длина) — перебираем 0..255 и
#     проверяем, при каком значении остаток файла разбирается до конца.
#
# Результат: <имя>.healed.json (пригоден для конвертации в .spine) и отчёт.
# Оригинал не изменяется никогда.
#
# Использование:
#   python3 skeleton_repair.py FILE_OR_DIR [...] [-o OUTDIR] [--report-only]
#   python3 skeleton_repair.py game/res --o healed --min-confidence 0.5
import argparse
import json
import os
import re
import struct
import sys

FFFD = b"\xef\xbf\xbd"

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if os.path.join(HERE, "spine_restore") not in sys.path:
    sys.path.insert(0, os.path.join(HERE, "spine_restore"))

from spine38_binary_to_json import Spine38BinaryReader  # noqa: E402

MAX_PARSE_BYTES = 300 * 1024 * 1024


class HealError(Exception):
    pass


def scan_units(data: bytes):
    """Юниты: реальный байт (int) или неизвестный ('u', индекс, варианты ширины).

    Python при decode(errors="replace") склеивает битую UTF-8-последовательность
    в ОДИН U+FFFD. Если перед ним lead-байт (0xC2..0xF4), то под ним скрыто
    2..4 исходных байт — поэтому ширину восстановления приходится подбирать.
    """
    units = []
    i = 0
    n = len(data)
    while i < n:
        if data.startswith(FFFD, i):
            units.append(("u", i, (1, 2, 3, 4)))
            i += 3
        else:
            units.append(data[i])
            i += 1
    return units


class HealReader(Spine38BinaryReader):
    """Парсер 3.8, который вместо потерянных байтов подставляет значения.

    Роли неизвестных байтов:
      float/int/string — значение не влияет на структуру, константа по контексту;
      var              — старший байт varint (счётчик/длина), подбирается перебором;
      byte             — байт вне varint (тип вложения, тип таймлайна), тоже перебор.
    """

    FILL = {"float": 0x00, "int": 0x00, "string": 0x3F, "var": 0x00, "byte": 0x00}

    def __init__(self, units, decisions=None):
        self.units = units
        self.upos = 0
        self.decisions = dict(decisions or {})
        self.unknown_bytes = 0
        self.var_unknowns = []
        self.byte_unknowns = []
        self.width_unknowns = []
        self.applied = []
        self.strings = []
        self.scale = 1.0
        self._pending = []
        self.last_unknown = None
        self.best_effort = False
        self.eof_zero = False
        self.soft_errors: list = []

    def _need(self, n: int) -> None:
        if self.upos + n > len(self.units):
            if self.eof_zero:
                return
            raise EOFError(f"нужно {n} байт на позиции {self.upos}, всего {len(self.units)}")

    def _unit(self, role: str) -> int:
        if self._pending:
            return self._pending.pop(0)
        if self.eof_zero and self.upos >= len(self.units):
            self.upos += 1
            return 0x00            # хвост файла: подставляем ноль вместо исключения
        self._need(1)
        u = self.units[self.upos]
        idx = self.upos
        self.upos += 1
        if isinstance(u, int):
            return u
        width, value = self.decisions.get(idx, (1, self.FILL.get(role, 0x00)))
        self.unknown_bytes += width
        if role in ("var", "byte"):
            self.last_unknown = (idx, role)
        if role == "var" and idx not in self.var_unknowns:
            self.var_unknowns.append(idx)
        elif role == "byte" and idx not in self.byte_unknowns:
            self.byte_unknowns.append(idx)
        if width != 1 and idx not in self.width_unknowns:
            self.width_unknowns.append(idx)
        self.applied.append((idx, width, value, role))
        self._pending = [0x00] * (width - 1)
        return value & 0xFF

    # ---- режим «дочитывания»: сбой внутри вложенной структуры не роняет весь файл ----
    def soft(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:            # noqa: BLE001
            self.soft_errors.append(f"{type(e).__name__}: {e}")
            if not self.best_effort:
                raise
            return None

    def read_byte(self) -> int:
        return self._unit("byte")

    def read_sbyte(self) -> int:
        b = self.read_byte()
        return b - 256 if b > 127 else b

    def read_boolean(self) -> bool:
        return self.read_byte() != 0

    def read_int(self) -> int:
        return struct.unpack(">i", bytes(self._unit("int") for _ in range(4)))[0]

    def read_int_var(self, optimize_positive: bool = True) -> int:
        b = self._unit("var")
        result = b & 0x7F
        if b & 0x80:
            b = self._unit("var")
            result |= (b & 0x7F) << 7
            if b & 0x80:
                b = self._unit("var")
                result |= (b & 0x7F) << 14
                if b & 0x80:
                    b = self._unit("var")
                    result |= (b & 0x7F) << 21
                    if b & 0x80:
                        result |= (self._unit("var") & 0x7F) << 28
        if not optimize_positive:
            result = (result >> 1) ^ -(result & 1)
        return result

    def read_float(self) -> float:
        v = struct.unpack(">f", bytes(self._unit("float") for _ in range(4)))[0]
        if v != v or v in (float("inf"), float("-inf")):
            return 0.0
        return v

    def read_string(self):
        count = self.read_int_var(True)
        if count == 0:
            return None
        if count == 1:
            return ""
        count -= 1
        if count > len(self.units):
            raise HealError(f"строка длиной {count} байт длиннее остатка файла")
        raw = bytes(self._unit("string") for _ in range(count))
        return raw.decode("utf-8", errors="replace")

    @property
    def pos(self) -> int:
        return self.upos

    @pos.setter
    def pos(self, v) -> None:
        self.upos = v


def make_soft_reader_class():
    """Класс чтения с tolerant-грамматикой (индексы проверяются)."""
    return tolerant_reader_class()


class SoftReader(HealReader):
    """HealReader, который не сдаётся на первом битом байте.

    Вложенные структуры (анимации, вложения скина) при сбое пропускаются,
    а разбор продолжается — так из сильно повреждённого файла получается
    пригодный для просмотра скелет: кости, слоты, уцелевшие анимации.
    """

    def _read_animation(self, *a, **kw):
        try:
            anim = super()._read_animation(*a, **kw)
        except Exception as e:            # noqa: BLE001
            self.soft_errors.append(f"animation: {type(e).__name__}: {e}")
            return None
        if anim and isinstance(anim, dict):
            name = anim.get("name") or "animation"
            self.kept.setdefault("animations", {})[name] = anim
        return anim

    def _read_attachment(self, *a, **kw):
        try:
            return super()._read_attachment(*a, **kw)
        except Exception as e:            # noqa: BLE001
            self.soft_errors.append(f"attachment: {type(e).__name__}: {e}")
            return None

    @property
    def kept(self) -> dict:
        if not hasattr(self, "_kept"):
            self._kept = {}
        return self._kept


def parse_once(units, decisions=None, best_effort=False):
    """Один проход грамматики. Всегда возвращает (json|None, reader, error|None)."""
    cls = make_soft_reader_class() if best_effort else SoftReader
    r = cls(units, decisions)
    r.best_effort = best_effort
    r.eof_zero = best_effort
    try:
        data = r.read_skeleton_data()
    except Exception as e:            # noqa: BLE001
        return None, r, f"{type(e).__name__}: {e}"
    return data, r, None


TAIL_RE = re.compile(r"нужно (\d+) байт на позиции (\d+), всего (\d+)")


def parse_tolerant(units, decisions=None):
    """Как parse_once, но допускает недостающие 1..3 байта в конце файла.

    Файлы Spine 3.8 заканчиваются нулевым байтом, а при подстановках ширины
    последний байт иногда «съедается» — это не повреждение, а артефакт чтения.
    """
    data, reader, err = parse_once(units, decisions)
    if data is not None:
        return data, reader, err
    m = TAIL_RE.search(err or "")
    if m and int(m.group(1)) <= 3 and int(m.group(2)) == int(m.group(3)):
        for pad in range(1, 4):
            data2, reader2, err2 = parse_once(list(units) + [0] * pad, decisions)
            if data2 is not None:
                return data2, reader2, err2
    return None, reader, err


def _tail_is_clean(units, pos, allow_tail: float = 0.0) -> bool:
    """После разбора допустим хвост из нулей/неизвестных байтов (или allow_tail доля)."""
    rest = units[pos:]
    if rest and allow_tail and len(rest) <= allow_tail * len(units):
        return True
    for u in rest:
        if isinstance(u, int) and u not in (0x00,):
            return False
        if isinstance(u, tuple) and len(rest) > 4096:
            return False
    return len(rest) <= 4096


def _plausible(data: dict) -> bool:
    """Санитарная проверка результата: скелет должен быть осмысленным."""
    skel = data.get("skeleton") or {}
    if not isinstance(skel, dict):
        return False
    if not (skel.get("spine") or "").startswith("3."):
        return False
    for key in ("bones", "slots", "skins", "animations"):
        v = data.get(key)
        if v is None:
            return False
    if not isinstance(data.get("bones"), list) or not data["bones"]:
        return False
    if not isinstance(data.get("animations"), (list, dict)):
        return False
    if not isinstance(data.get("slots"), list) or not data["slots"]:
        return False
    return True


SORRY = (IndexError, ValueError, KeyError, TypeError, EOFError, HealError,
         ZeroDivisionError, struct.error, UnicodeDecodeError)


def _forced_candidates(idx, tried, limit=24):
    """Значения, которые правдоподобны для индекса/счётчика: 0..12, затем шире."""
    vals = [0] + list(range(1, 13)) + list(range(13, 64)) + list(range(0x80, 0x100))
    out = []
    for v in vals:
        if (1, v) not in tried:
            out.append((1, v))
        if len(out) >= limit:
            break
    return out


def heal(data: bytes, budget: int = 3000, min_unknown_pct: float = 0.0, hints=None,
         allow_tail: float = 0.0):
    """Лечит файл, подбирая значения потерянных байтов.

    Оракул — исключение парсера: IndexError означает «индекс вышел за границы»,
    EOFError — «разбор дошёл до конца», ValueError — «неизвестный тип». Всё это
    указывает на конкретный испорченный байт, поэтому перебор точечный.
    """
    fffd = data.count(FFFD)
    report = {
        "bytes": len(data),
        "fffd_markers": fffd,
        "fffd_pct": round(fffd * 3 * 100.0 / max(1, len(data)), 2),
        "healed": False,
        "confidence": 0.0,
        "decisions": [],
        "error": "",
    }
    if fffd == 0:
        report["error"] = "повреждений U+FFFD нет"
        return None, report
    if fffd * 3 * 100.0 / max(1, len(data)) < min_unknown_pct:
        report["error"] = "слишком много повреждений"
        return None, report

    units = scan_units(data)
    unks = [i for i, u in enumerate(units) if isinstance(u, tuple)]
    report["units"] = len(units)
    report["unknown_groups"] = len(unks)
    tried = {i: set() for i in unks}
    forced: dict[int, tuple] = {}
    repair_state: dict = {}
    merged = dict(cached_hints(data))
    merged.update(hints or {})
    for idx, w in merged.items():
        if idx in tried:
            forced[idx] = (int(w), 0x00)
            report["hints_applied"] = len(forced)
    attempts = 0
    stall = 0

    parsed = reader = None
    err = ""
    while attempts < budget:
        parsed, reader, err = parse_tolerant(units, forced)
        attempts += 1
        if parsed is not None and _plausible(parsed) and _tail_is_clean(units, reader.upos, allow_tail):
            break
        if reader is None or reader.last_unknown is None:
            report["error"] = err or "неизвестно, где сломалось"
            parsed = None
            break
        idx, role = reader.last_unknown
        if idx in forced:
            tried[idx].add(forced[idx])
        progressed = False

        # Признак сбитого выравнивания: разбор дошёл ровно до конца файла.
        # Тогда чинить значения бессмысленно — ищем комбинации ширин лучом.
        if TAIL_RE.search(err or ""):
            base_pos = reader.upos if reader else 0
            pool = [i for i, u in enumerate(units) if isinstance(u, tuple) and i < base_pos]
            beam = [dict(forced)]
            for _depth in range(1, 4):
                scored = []
                for st in beam:
                    extra = []
                    for j in pool:
                        if j in st:
                            continue
                        for w in (2, 3, 4):
                            if attempts >= budget:
                                break
                            t = dict(st)
                            t[j] = (w, 0x00)
                            p2, r2, _e2 = parse_once(units, t)
                            attempts += 1
                            if p2 is not None and _plausible(p2) and _tail_is_clean(units, r2.upos, allow_tail):
                                forced = t
                                parsed, reader = p2, r2
                                progressed = True
                                break
                            scored.append((r2.upos if r2 else -1, t))
                        if progressed or attempts >= budget:
                            break
                    # рядом с концом файла часто стоит счётчик секции (например,
                    # число анимаций) — пробуем его увеличить/уменьшить
                    for idx in (reader.var_unknowns[-6:] if reader else []):
                        if idx in st or idx in extra:
                            continue
                        for v in (1, 2, 3, 4, 5, 6):
                            if attempts >= budget:
                                break
                            t = dict(st)
                            t[idx] = (1, v)
                            extra.append(idx)
                            p2, r2, _e3 = parse_once(units, t)
                            attempts += 1
                            if p2 is not None and _plausible(p2) and _tail_is_clean(units, r2.upos, allow_tail):
                                forced = t
                                parsed, reader = p2, r2
                                progressed = True
                                break
                            scored.append((r2.upos if r2 else -1, t))
                        if progressed or attempts >= budget:
                            break
                    if progressed or attempts >= budget:
                        break
                if progressed or attempts >= budget:
                    break
                if not scored:
                    break
                scored.sort(key=lambda x: -x[0])
                # разнообразие: лучшее состояние для каждого «первого» исправленного
                # байта, иначе луч остаётся на одной неверной ветке
                seen_first = set()
                beam = []
                for _g, t in scored:
                    key = min(t) if t else -1
                    if key in seen_first:
                        continue
                    seen_first.add(key)
                    beam.append(t)
                    if len(beam) >= 12:
                        break
            if progressed:
                stall = 0
                continue
            if beam:
                best_gain, best_state = -1, None
                for st in beam:
                    p2, r2, _e2 = parse_once(units, st)
                    attempts += 1
                    if r2 and r2.upos > best_gain:
                        best_gain, best_state = r2.upos, st
                if best_state is not None:
                    forced = best_state
                    reader, err = parse_once(units, forced)[1:]
                    attempts += 1
                    stall = 0
                    continue

        for cand in _forced_candidates(idx, tried[idx]):
            if attempts >= budget:
                break
            trial = dict(forced)
            trial[idx] = cand
            tried[idx].add(cand)
            p2, r2, e2 = parse_once(units, trial)
            attempts += 1
            if p2 is not None and _plausible(p2) and _tail_is_clean(units, r2.upos, allow_tail):
                forced = trial
                parsed, reader = p2, r2
                progressed = True
                break
            if r2 is not None and r2.upos > (reader.upos if reader else 0) + 1:
                forced = trial          # разбор ушёл дальше — оставляем
                progressed = True
                break
        if progressed:
            stall = 0
            continue
        stall += 1
        if stall == 1 and not repair_state.get("counts_done"):
            # Фаза «счётчики»: перебираем увеличение/уменьшение значений всех
            # структурных байтов — лечит случаи, когда разбор ушёл в конец файла
            # из-за одного неверного количества (анимаций, таймлайнов, костей).
            repair_state["counts_done"] = True
            for idx in list(reader.var_unknowns if reader else []):
                for v in list(range(0, 10)) + [16, 24, 32, 48, 64, 96, 128]:
                    if attempts >= budget:
                        break
                    trial = dict(forced)
                    trial[idx] = (1, v)
                    p2, r2, _e2 = parse_once(units, trial)
                    attempts += 1
                    if p2 is not None and _plausible(p2) and _tail_is_clean(units, r2.upos, allow_tail):
                        forced = trial
                        parsed, reader = p2, r2
                        progressed = True
                        break
                if progressed or attempts >= budget:
                    break
        # подозрение на сбитое выравнивание: один U+FFFD мог скрыть 2..4 байта.
        # расширяем ближайшие неизвестные и берём тот, при котором разбор идёт дальше.
        base = reader.upos if reader else 0
        _all_near = [i for i, u in enumerate(units) if isinstance(u, tuple) and i < base]
        widen_round = repair_state.get("widen_round", 0) + 1
        repair_state["widen_round"] = widen_round
        near = _all_near[-12:] if widen_round <= 2 else _all_near
        best_gain, best = 0, None
        for j in near:
            for w in units[j][2]:
                if w == 1 or (w, 0x00) in tried[j]:
                    continue
                if attempts >= budget:
                    break
                trial = dict(forced)
                trial[j] = (w, 0x00)
                tried[j].add((w, 0x00))
                p2, r2, _e2 = parse_tolerant(units, trial)
                attempts += 1
                if p2 is not None and _plausible(p2) and _tail_is_clean(units, r2.upos, allow_tail):
                    forced = trial
                    parsed, reader = p2, r2
                    progressed = True
                    break
                gain = r2.upos if r2 else -1
                if gain > best_gain:
                    best_gain, best = gain, (j, (w, 0x00))
            if progressed:
                break
        if not progressed and best is not None:
            forced[best[0]] = best[1]
            tried[best[0]].add(best[1])
            progressed = True
        if not progressed:
            report["error"] = err or "перебор не дал прогресса"
            parsed = None
            break
            parsed = None
            break

    report["attempts"] = attempts
    if parsed is None or reader is None or not _plausible(parsed):
        report["error"] = report["error"] or err or "не удалось подобрать значения"
        report["decisions"] = [{"unit": k, "width": w, "value": v}
                               for k, (w, v) in sorted(forced.items())]
        return None, report

    report["healed"] = True
    learn_hints(data, forced)
    report["decisions"] = [{"unit": k, "width": w, "value": v}
                           for k, (w, v) in sorted(forced.items())]
    report["unknown_bytes_filled"] = reader.unknown_bytes
    report["consumed_units"] = reader.upos
    report["bones"] = len(parsed.get("bones") or [])
    report["slots"] = len(parsed.get("slots") or [])
    anims = parsed.get("animations")
    report["animations"] = len(anims) if isinstance(anims, (list, dict)) else 0
    skins = parsed.get("skins")
    report["skins"] = len(skins) if isinstance(skins, (list, dict)) else 0
    report["restored_bytes"] = reader.unknown_bytes

    structural = len(reader.var_unknowns) + len(reader.byte_unknowns)
    widened = len(reader.width_unknowns)
    total = max(1, reader.unknown_bytes)
    conf = 1.0 - min(1.0, (structural * 1.5 + widened * 0.6) / total)
    if not parsed.get("animations") and not report["animations"]:
        conf *= 0.5
    report["confidence"] = round(max(0.0, min(1.0, conf)), 2)
    report["structural_unknowns"] = structural
    report["width_fixes"] = widened
    return sanitize(parsed), report


CTRL = {c for c in range(32)} - {9, 10, 13}


def _clean_name(name, fallback):
    """Имя без управляющих символов — иначе редактор не импортирует скелет."""
    if not isinstance(name, str):
        return fallback
    out = "".join(ch for ch in name if ord(ch) not in CTRL).strip()
    return out or fallback


def sanitize(doc):
    """Приводит восстановленный скелет к виду, который принимает редактор."""
    def num(v):
        return isinstance(v, (int, float)) and v == v and v not in (float("inf"), float("-inf"))

    def fix_curve(tl):
        c = tl.get("curve")
        if c is None:
            for k in ("c2", "c3", "c4"):
                tl.pop(k, None)
            return
        if not num(c):
            c = 0
        c = max(0.0, min(1.0, float(c)))
        tl["curve"] = c
        if c <= 0 or c >= 1:
            for k in ("c2", "c3", "c4"):
                tl.pop(k, None)
        else:
            for k, dflt in (("c2", 0.0), ("c3", 0.0), ("c4", 1.0)):
                v = tl.get(k, dflt)
                tl[k] = float(v) if num(v) else dflt

    def walk(node):
        if isinstance(node, dict):
            if "curve" in node and ("time" in node or "x" in node):
                fix_curve(node)
            for val in node.values():
                walk(val)
        elif isinstance(node, list):
            for val in node:
                walk(val)

    skel = doc.get("skeleton")
    if isinstance(skel, dict):
        skel["spine"] = _clean_name(skel.get("spine"), "3.8.99")
    for i, b in enumerate(doc.get("bones") or []):
        if isinstance(b, dict):
            b["name"] = _clean_name(b.get("name"), "bone%d" % i)
    for i, sl in enumerate(doc.get("slots") or []):
        if isinstance(sl, dict):
            sl["name"] = _clean_name(sl.get("name"), "slot%d" % i)
            sl["bone"] = _clean_name(sl.get("bone"), "root")
    anims = doc.get("animations")
    if isinstance(anims, dict):
        doc["animations"] = {(_clean_name(k, "animation")): v for k, v in anims.items()}
    walk(doc.get("animations"))
    return doc


def align_repair(units, time_limit: float = 100.0, allow_tail: float = 0.0, beam: int = 40):
    """Восстановление выравнивания перебором комбинаций.

    Один U+FFFD скрывает 1..4 исходных байта, из-за чего разбор разходится с
    реальностью. Состояние поиска — назначенная ширина для каждого неизвестного
    байта; мера прогресса — позиция разбора. Из состояния пробуем все варианты
    для первого нерешённого байта перед точкой сбоя, лучшие состояния ведём
    дальше (луч поиска). Возвращает (json|None, reader|None, инфо).
    """
    import time as _t
    t0 = _t.monotonic()
    unks = [i for i, u in enumerate(units) if isinstance(u, tuple)]
    if not unks:
        return None, None, {"error": "нет неизвестных байтов"}
    seen = set()
    queued = set()
    attempts = 0

    def run(state):
        nonlocal attempts
        attempts += 1
        p, r, e = parse_tolerant(units, state)
        if p is not None and _plausible(p) and _tail_is_clean(units, r.upos, allow_tail):
            return "ok", r.upos, p, r
        return "bad", (r.upos if r else -1), None, e

    def done(p, r, st):
        return p, r, {"attempts": attempts, "fixes": len(st),
                      "seconds": round(_t.monotonic() - t0, 1)}

    frontier = [{}]
    while frontier and _t.monotonic() - t0 < time_limit:
        scored = []
        for st in frontier:
            kk = tuple(sorted(st.items()))
            if kk in seen:
                continue
            seen.add(kk)
            status, pos, p_obj, r_obj = run(st)
            if status == "ok":
                return done(p_obj, r_obj, st)
            if _t.monotonic() - t0 >= time_limit:
                break
            scope = [j for j in unks if j < pos and j not in st]
            if scope:
                j = scope[0]
                for w in (1, 2, 3, 4):
                    t2 = dict(st)
                    if w != 1:
                        t2[j] = (w, 0x00)
                    nxt_key = tuple(sorted(t2.items()))
                    if nxt_key in seen or nxt_key in queued:
                        continue
                    queued.add(nxt_key)
                    s2, p2, o2, x2 = run(t2)
                    if s2 == "ok":
                        return done(p2, o2, t2)
                    scored.append((p2, t2))
            else:
                prev = sorted(st)
                if not prev:
                    continue
                j = prev[-1]
                for w in (2, 3, 4):
                    t2 = dict(st)
                    t2[j] = (w, 0x00)
                    nxt_key = tuple(sorted(t2.items()))
                    if nxt_key in seen or nxt_key in queued:
                        continue
                    queued.add(nxt_key)
                    s2, p2, o2, x2 = run(t2)
                    if s2 == "ok":
                        return done(p2, o2, t2)
                    scored.append((p2, t2))
        if not scored:
            break
        scored.sort(key=lambda x: -x[0])
        frontier = [st for _pos, st in scored[:beam]]
    return None, None, {"error": "комбинации не нашлись", "attempts": attempts,
                        "seconds": round(_t.monotonic() - t0, 1)}


SAFE_PATCHES = (
    ('bone["parent"] = bones[parent_idx]["name"]',
     'bone["parent"] = bones[parent_idx]["name"] if 0 <= parent_idx < len(bones) else (bones[-1]["name"] if bones else "root")'),
    ('"bone": bones[bone_idx]["name"],',
     '"bone": bones[bone_idx]["name"] if 0 <= bone_idx < len(bones) else (bones[0]["name"] if bones else "root"),'),
    ('slot_name = slots[slot_idx]["name"]',
     'slot_name = slots[slot_idx]["name"] if 0 <= slot_idx < len(slots) else (slots[0]["name"] if slots else "")'),
    ('bone_name = bones[bone_idx]["name"]',
     'bone_name = bones[bone_idx]["name"] if 0 <= bone_idx < len(bones) else (bones[0]["name"] if bones else "root")'),
    ('"slot": slots[slot_idx]["name"]',
     '"slot": slots[slot_idx]["name"] if 0 <= slot_idx < len(slots) else (slots[0]["name"] if slots else "")'),
)


def _load_tolerant_grammar():
    """Грамматика 3.8 с проверяемыми индексами: битый индекс не роняет разбор.

    Класс собирается как подкласс HealReader (примитивы чтения с подстановкой
    неизвестных байтов), но с методами грамматики из пропатченного исходника.
    """
    import types
    path = os.path.join(HERE, "spine_restore", "spine38_binary_to_json.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    for old, new in SAFE_PATCHES:
        src = src.replace(old, new)
    # оставшиеся «списочные» индексы из read_int_var — заменяем на безопасный вид
    import re as _re
    src = _re.sub(r"(\w+)\[self\.read_int_var\(True\)\]\[.name.\]",
                  r'_safe(\1, self.read_int_var(True))', src)
    src = _re.sub(r"\[(\w+)\[self\.read_int_var\(True\)\]\[.name.\] for _ in range\((\w+)\)\]",
                  r'[_safe(\1, self.read_int_var(True)) for _ in range(\2)]', src)
    helper = """
def _safe(seq, idx, default="root"):
    try:
        return seq[idx]["name"] if 0 <= idx < len(seq) else default
    except Exception:
        return default
"""
    src = src.replace("class Spine38BinaryReader:", helper + "\n\nclass Spine38BinaryReader:", 1)
    mod = types.ModuleType("spine38_tolerant")
    mod.__file__ = path
    exec(compile(src, path, "exec"), mod.__dict__)
    patched = mod.Spine38BinaryReader
    # примитивы чтения остаются от HealReader (в них подставляются неизвестные байты)
    own = {"_need", "read_byte", "read_sbyte", "read_boolean", "read_int", "read_int_var",
           "read_float", "read_string", "read_string_ref", "read_color_rgba",
           "read_skeleton_data", "pos"}
    methods = {k: v for k, v in patched.__dict__.items()
               if callable(v) and not k.startswith("__") and k not in own}
    methods["read_skeleton_data"] = patched.__dict__["read_skeleton_data"]
    return type("TolerantGrammar", (HealReader,), methods)


TolerantGrammar = None


def tolerant_reader_class():
    global TolerantGrammar
    if TolerantGrammar is None:
        TolerantGrammar = _load_tolerant_grammar()
    return TolerantGrammar


HINTS_CACHE = os.path.join(HERE, "repair_hints.json")


def load_hint_cache() -> dict:
    try:
        with open(HINTS_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_hint_cache(cache: dict) -> None:
    try:
        with open(HINTS_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1, sort_keys=True)
    except OSError:
        pass


def file_digest(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()[:16]


def cached_hints(data: bytes) -> dict:
    """Сохранённые вручную правки ширин для этого файла (ключ — хеш)."""
    entry = load_hint_cache().get(file_digest(data))
    if not entry:
        return {}
    return {int(k): int(v) for k, v in entry.items()}


def learn_hints(data: bytes, decisions: dict) -> None:
    if not decisions:
        return
    cache = load_hint_cache()
    cache[file_digest(data)] = {str(k): v[0] for k, v in decisions.items() if v[0] != 1}
    save_hint_cache(cache)


def diagnose(path: str) -> dict:
    with open(path, "rb") as f:
        data = f.read()
    fffd = data.count(FFFD)
    info = {
        "path": path,
        "bytes": len(data),
        "fffd_markers": fffd,
        "fffd_pct": round(fffd * 3 * 100.0 / max(1, len(data)), 2),
        "tail_byte": data[-1] if data else None,
    }
    if fffd == 0:
        info["class"] = "clean"
        return info
    info["class"] = "fffd-replaced"
    return info


def collect_targets(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _d, files in os.walk(p):
                for fn in sorted(files):
                    if fn.lower().endswith((".skel", ".json", ".bin")):
                        out.append(os.path.join(root, fn))
        elif os.path.isfile(p):
            out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Лечение Spine .skel, повреждённых U+FFFD")
    ap.add_argument("paths", nargs="+", help="файлы или папки")
    ap.add_argument("-o", "--out", default=None, help="куда класть .healed.json (по умолчанию рядом)")
    ap.add_argument("--report", default=None, help="файл с общим отчётом (json)")
    ap.add_argument("--budget", type=int, default=3000, help="лимит попыток перебора на файл")
    ap.add_argument("--min-confidence", type=float, default=0.0,
                    help="не сохранять результат с уверенностью ниже порога")
    ap.add_argument("--dry-run", action="store_true", help="только диагностика, не лечить")
    ap.add_argument("--allow-tail", type=float, default=0.0,
                    help="допустимый непрочитанный хвост, доля файла (0.2 = 20%%)")
    ap.add_argument("--hints", default="",
                    help="принудительные ширины: '919=2,2476=4' (индексы юнитов из отчёта)")
    args = ap.parse_args()

    hints = {}
    if args.hints:
        for part in args.hints.replace(";", ",").split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            try:
                hints[int(k)] = int(v)
            except ValueError:
                pass
        print("hints:", ", ".join(f"{k}:{v}" for k, v in sorted(hints.items())))

    targets = collect_targets(args.paths)
    if not targets:
        print("нечего делать: подходящих файлов не найдено")
        return 1

    rows = []
    healed = 0
    for path in targets:
        info = diagnose(path)
        row = {"file": os.path.basename(path), "path": path, **info}
        if args.dry_run or info["class"] != "fffd-replaced":
            rows.append(row)
            continue
        if info["bytes"] > MAX_PARSE_BYTES:
            row["result"] = "пропущен: файл слишком большой"
            rows.append(row)
            continue
        with open(path, "rb") as f:
            data = f.read()
        parsed, rep = heal(data, budget=args.budget, hints=hints,
                             allow_tail=args.allow_tail)
        row["result"] = rep.get("error") or ""
        row["healed"] = rep["healed"]
        row["confidence"] = rep["confidence"]
        if parsed is not None:
            row["bones"] = rep.get("bones")
            row["slots"] = rep.get("slots")
            row["animations"] = rep.get("animations")
            row["skins"] = rep.get("skins")
            row["decisions"] = len(rep.get("decisions") or [])
            if rep["confidence"] >= args.min_confidence:
                dst_dir = args.out or os.path.dirname(os.path.abspath(path))
                os.makedirs(dst_dir, exist_ok=True)
                stem = os.path.splitext(os.path.basename(path))[0]
                out_json = os.path.join(dst_dir, stem + ".healed.json")
                with open(out_json, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, ensure_ascii=False, indent=1)
                row["output"] = out_json
                healed += 1
        rows.append(row)
        print(f"{row['file']:<38} {info['fffd_pct']:>6}%  "
              f"{'вылечен' if row.get('healed') else 'не поддаётся':<14} "
              f"увер={row.get('confidence', 0):.2f} "
              f"кости={row.get('bones', '-')} аним={row.get('animations', '-')} "
              f"решений={row.get('decisions', '-')} {row.get('result', '')}")

    print(f"\nфайлов: {len(rows)}, вылечено: {healed}")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print("отчёт:", args.report)
    return 0 if healed or args.dry_run else 2


if __name__ == "__main__":
    sys.exit(main())
