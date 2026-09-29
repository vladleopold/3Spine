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
    if isinstance(skeleton, dict):
        skeleton["spine"] = WEB_VERSION
    else:
        out["skeleton"] = {"spine": WEB_VERSION}

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

    animations = out.get("animations") or {}
    for anim in animations.values():
        if not isinstance(anim, dict):
            continue

        slots = anim.get("slots")
        if isinstance(slots, dict):
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
        if isinstance(transform, list):
            for item in transform:
                if isinstance(item, dict) and "bone" not in item:
                    name = item.get("name")
                    if name:
                        rebuilt = {"bone": name}
                        rebuilt.update({k: v for k, v in item.items() if k != "name"})
                        item.clear()
                        item.update(rebuilt)

    return out


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
