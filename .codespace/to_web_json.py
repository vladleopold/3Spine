#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Готовит web-JSON из скелета Spine 3.8 — одна логика для всех игр.

Зачем: браузер играет скелеты рантаймом 4.x, а 4.x и 3.8 расходятся в имени
цветового таймлайна слота. В 3.8 он называется "color", в 4.x — "rgba".
Рантайм 4.x читает только "rgba", поэтому 3.8-цвета отбрасываются молча: слоты
остаются с альфой из setup-позы (часто 0) и картинка не рисуется вообще.
Скелеты 3.8.99 из 78 файлов Playson содержат 8296 таких таймлайнов.

Правила (всё остальное в форматах 3.8 и 4.x совпадает, см. проверку ниже):
  * animations[].slots[<name>].color  ->  .rgba   (кадры оставляем как есть:
    в 4.x кадр rgba — тот же {time, color, curve}, читается Color.fromString
    и readCurve, где кривая это "stepped" либо плоский массив из 4 чисел);
  * animations[].transform[i] обязателен с полем bone — дорисовываем из имени,
    если редактор 3.8 его не записал;
  * skeleton.spine ставим в версию рантайма, которым будем играть, иначе фронт
    выберет 3.8-сборку, в которой нет рабочего канвас-рендерера.

Использование:
    python3 to_web_json.py <каталог со скелетами.json> <каталог для web-json>
"""

import json
import os
import re
import sys

WEB_VERSION = "4.0.31"          # рантайм, на котором подтверждено воспроизведение


def hex_to_rgba(value: str):
    """'ffffff00' -> (1,1,1,0). Только для проверок и логов, формат не меняем."""
    try:
        s = str(value).strip().lstrip("#")
        if len(s) == 6:
            s += "ff"
        if len(s) != 8:
            return None
        return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4, 6))
    except (ValueError, TypeError):
        return None


def convert(data: dict) -> dict:
    """Возвращает копию скелета в формате, который читает рантайм 4.x."""
    out = json.loads(json.dumps(data))          # глубокая копия без общих ссылок

    skeleton = out.get("skeleton")
    # 3.5-3.8 приводим к 4.0.31 (единственный формат, цвет которого переименован,
    # и рантайм, на котором подтверждено воспроизведение). Данные 4.x оставляем в
    # СВОЕМ формате: в 4.1 цвет слота снова называется "color" (4.0 читал "rgba"),
    # там есть физика, а понижение версии всё это тихо выкинуло бы. Сайт сам
    # выберет нужный рантайм по полю skeleton.spine (4.0 -> 4.0.31, 4.1 -> 4.1.55).
    src_ver = ""
    if isinstance(skeleton, dict):
        src_ver = str(skeleton.get("spine", "") or "")
    m = re.match(r"^(\d+)\.(\d+)", src_ver)
    native_4x = bool(m) and (int(m.group(1)), int(m.group(2))) >= (4, 0)
    target_ver = src_ver if native_4x else WEB_VERSION

    if isinstance(skeleton, dict):
        skeleton["spine"] = target_ver
    else:
        out["skeleton"] = {"spine": target_ver}

    # Рантайм 4.0.x ждёт скины МАССИВОМ вида
    #   [{ "name": "default", "attachments": { "<слот>": { "<имя>": {...} } } }]
    # А часть игр (в том числе весь PragmaticPlay UHT) отдаёт скины объектом
    # { "<имя>": { "<слот>": { "<вложение>": {...} } } }. С таким объектом
    # рантайм молча не создаёт ни одного скина (root.skins.length === 0), и
    # первая же анимация с deform падает на findSkin() → null.getAttachment().
    skins = out.get("skins")
    if isinstance(skins, dict):
        out["skins"] = [{"name": name, "attachments": attachments}
                        for name, attachments in skins.items()]

    # Анимации: в 3.x это ОБЪЕКТ { "<имя>": {...} }, в 4.x — МАССИВ
    # [{ "name": "<имя>", ... }]. Рантайм 4.x читает root.animations[i], и с
    # объектом animations.length === undefined, то есть анимаций НОЛЬ: карточка
    # показывает только позу покоя и не проигрывает ничего (а если поза пустая —
    # выглядит как пустая заглушка). Весь PragmaticPlay UHT отдаёт объект.
    anims = out.get("animations")
    if isinstance(anims, dict):
        fixed = []
        for name, body in anims.items():
            if isinstance(body, dict):
                item = dict(body)
                item.setdefault("name", name)
                fixed.append(item)
        out["animations"] = fixed

    for anim in (out.get("animations") or []):
        if not isinstance(anim, dict):
            continue

        slots = anim.get("slots")
        if isinstance(slots, dict) and not native_4x:
            for entry in slots.values():
                if isinstance(entry, dict) and "color" in entry:
                    # порядок ключей сохраняем: 4.x ждёт rgba рядом с attachment
                    rebuilt = {}
                    for key, val in entry.items():
                        if key == "color":
                            rebuilt["rgba"] = val
                        else:
                            rebuilt[key] = val
                    entry.clear()
                    entry.update(rebuilt)

        transform = anim.get("transform")
        if isinstance(transform, list) and not native_4x:
            for item in transform:
                if isinstance(item, dict) and "bone" not in item:
                    name = item.get("name")
                    if name:
                        rebuilt = {"bone": name}
                        rebuilt.update({k: v for k, v in item.items() if k != "name"})
                        item.clear()
                        item.update(rebuilt)

    out["_кривых_исправлено"] = 0
    _fix_curves(out)
    _fix_curve_widths(out)
    out.pop("_кривых_исправлено", None)

    return out


def _fix_curves(node) -> int:
    """Переводит БЕЗИЕРЫ 3.x в формат 4.x: curve: [cx1, cy1, cx2, cy2].

    В 3.x кадр таймлайна описывал кривую четырьмя отдельными полями —
    ``curve`` (число) и ``c2``/``c3``/``c4``. В 4.x рантайм ждёт ``curve``
    МАССИВОМ из четырёх чисел и читает его так::

        let curve = keyMap.curve;
        if (curve) { let i = value << 2; let cx1 = curve[i]; ... }

    Со скаляром на месте ``curve[0]`` — это undefined, bezier получает NaN, и
    всё, что считается по этой кривой (поворот/масштаб/цвет кости и слота,
    деформация), становится NaN. Кран скелета не бросает ошибку — вершины мешей
    уезжают в NaN, треугольники рисуются «в никуда», и карточка молча остаётся
    пустой. Проверено на PragmaticPlay vs20wraanu: 255 кривых в wran_hv_1,
    после правки карточка рисует 66 101 пиксель вместо нуля.

    Данные 4.x приходят уже с массивами, там функция ничего не меняет.
    """
    fixed = 0
    if isinstance(node, dict):
        curve = node.get("curve")
        if isinstance(curve, str):
            # 3.x помечал «ступенчатую» интерполяцию строкой. Рантайм 4.x про
            # любой непустой curve читает как массив чисел, строка даёт
            # undefined → NaN в позе → пустая карточка. Заменяем на bezier
            # [0,0,0,0]: значение держится до конца интервала и прыгает на
            # следующем кадре — визуально та же ступенька.
            node["curve"] = [0, 0, 0, 0]
            fixed += 1
        elif (isinstance(curve, (int, float)) and not isinstance(curve, bool)
                and "c2" in node and "c3" in node and "c4" in node):
            node["curve"] = [curve, node["c2"], node["c3"], node["c4"]]
            for key in ("c2", "c3", "c4"):
                node.pop(key, None)
            fixed += 1
        for value in node.values():
            fixed += _fix_curves(value)
    elif isinstance(node, list):
        for value in node:
            fixed += _fix_curves(value)
    return fixed


# Сколько значений в таймлайне: в 4.x на каждое значение — свои четыре числа
# кривой (readCurve читает curve[value << 2 .. +3]). В 3.x кривая одна на весь
# кадр, поэтому для многозначных таймлайнов её надо продублировать.
_TIMELINE_VALUES = {
    "rotate": 1, "transform": 1, "deform": 1, "ik": 1, "attachment": 0,
    "drawOrder": 0, "event": 0, "path": 2,
    "translate": 2, "scale": 2, "shear": 2,
    "rgba": 4, "color": 4,
}


def _fix_curve_widths(data) -> int:
    """Дублирует кривую на каждое значение таймлайна (иначе вторая половина
    bezier читается как undefined → NaN в позе → карточка пустая)."""
    fixed = 0
    anims = data.get("animations") or []
    # 3.x отдаёт анимации объектом {имя: {...}}, 4.x — массивом. convert() уже
    # приводит к массиву, но функцию зовут и на сырых данных.
    if isinstance(anims, dict):
        anims = list(anims.values())
    for anim in anims:
        if not isinstance(anim, dict):
            continue
        groups = (
            (anim.get("bones") or {}, None),
            (anim.get("slots") or {}, None),
        )
        for group, _ in groups:
            for timelines in group.values():
                if not isinstance(timelines, dict):
                    continue
                for kind, keys in timelines.items():
                    n = _TIMELINE_VALUES.get(kind)
                    if not n or not isinstance(keys, list):
                        continue
                    for key in keys:
                        if not isinstance(key, dict):
                            continue
                        curve = key.get("curve")
                        if isinstance(curve, list) and len(curve) == 4 and n > 1:
                            key["curve"] = curve * n
                            fixed += 1
        for attach, keyed in (anim.get("deform") or {}).items():
            for keyed2 in (keyed or {}).values():
                for keys in (keyed2 or {}).values():
                    for key in (keys or []):
                        if isinstance(key, dict) and isinstance(key.get("curve"), list) \
                                and len(key["curve"]) == 4:
                            fixed += 0  # деформация — одно значение, без дублей
    return fixed


def convert_file(src: str, dst: str) -> dict:
    with open(src, "rb") as f:
        data = json.loads(f.read().decode("utf-8", "ignore"))
    stats = count_issues(data)
    web = convert(data)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(web, f, ensure_ascii=False, separators=(",", ":"))
    stats["цветовых_таймлайнов"] = count_rgba(web)
    return stats


def count_issues(data: dict) -> dict:
    """Сколько всего 3.8-цветовых таймлайнов и сколько слотов с нулевой альфой."""
    colors = 0
    transparent = 0
    slots_total = 0
    for slot in (data.get("slots") or []):
        slots_total += 1
        rgba = hex_to_rgba(slot.get("color", "ffffffff"))
        if rgba and rgba[3] == 0:
            transparent += 1
    for anim in (data.get("animations") or {}).values():
        if not isinstance(anim, dict):
            continue
        for entry in (anim.get("slots") or {}).values():
            if isinstance(entry, dict) and "color" in entry:
                colors += len(entry["color"] or [])
    return {"слотов": slots_total, "прозрачных_слотов": transparent,
            "кадров_цвета_в_3x": colors}


def count_rgba(data: dict) -> int:
    total = 0
    for anim in (data.get("animations") or {}).values():
        if not isinstance(anim, dict):
            continue
        for entry in (anim.get("slots") or {}).values():
            if isinstance(entry, dict) and "rgba" in entry:
                total += len(entry["rgba"] or [])
    return total


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 1
    src_dir, dst_dir = argv[1], argv[2]
    os.makedirs(dst_dir, exist_ok=True)
    made = 0
    for base, _dirs, names in os.walk(src_dir):
        for name in sorted(names):
            if not name.lower().endswith(".json"):
                continue
            full = os.path.join(base, name)
            try:
                stats = convert_file(full, os.path.join(dst_dir, name))
            except (OSError, ValueError) as e:
                print(f"web-json: пропущен {full}: {e}")
                continue
            if not stats["кадров_цвета_в_3x"] and not stats["цветовых_таймлайнов"]:
                continue                      # без цветов конвертировать нечего
            made += 1
            print(f"web-json: {name}: цветовых кадров {stats['кадров_цвета_в_3x']} -> "
                  f"{stats['цветовых_таймлайнов']} (слотов {stats['слотов']}, "
                  f"из них прозрачных {stats['прозрачных_слотов']})")
    print(f"web-json: готово файлов {made} в {dst_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
