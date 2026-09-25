Spine Restore Tool
==================
Конвертер binary Spine 3.8 → читаемый JSON

Подходит для файлов:
  • .skel
  • .json  (на самом деле binary skeleton, просто переименованный skel)
  • .skel.bytes

Основан на официальном runtime EsotericSoftware spine-runtimes ветка 3.8
(SkeletonBinary C# / Java).


ЧТО ДЕЛАЕТ
----------
1. Определяет binary Spine (по маркеру версии 3.8.x / 3.7.x / 4.x)
2. Проверяет «битость»: если в файле есть UTF-8 replacement (EF BF BD = U+FFFD),
   файл помечен как CORRUPTED — восстановить данные нельзя
3. Чистые binary конвертирует в обычный Spine JSON (bones, slots, skins, animations)


УСТАНОВКА
---------
Нужен только Python 3.9+ (без внешних пакетов для конвертера).

  cd SpineRestoreTool/bin
  python3 spine_restore.py --help


ПРИМЕРЫ
-------
# Один файл
python3 spine_restore.py ../examples/plum.json -o plum_readable.json

# Вся папка
python3 spine_restore.py /path/to/folder -o /path/to/output

# Только проверка (кто чистый, кто битый)
python3 spine_restore.py /path/to/folder --check-only

# GUI (macOS / Windows / Linux, нужен tkinter)
python3 spine_batch_converter.py


ПРО «БИТЫЕ» ФАЙЛЫ (CORRUPTED)
-----------------------------
Если binary открыли как текст (редактор, git без binary, неверный extract)
и сохранили — каждый «невалидный» байт заменился на U+FFFD (байты EF BF BD).

Признаки:
  • python3 spine_restore.py file.json --check-only  →  CORRUPTED
  • FFFD count > 0

Такие файлы ВОССТАНОВИТЬ НЕЛЬЗЯ: исходные байты уже потеряны.
Нужно заново выгрузить binary из игры / CDN / Unity (raw, без text encoding).

Чистые binary (FFFD=0) конвертируются нормально — см. examples/.


СТРУКТУРА
---------
SpineRestoreTool/
  README.txt
  bin/
    spine_restore.py           ← CLI (главный инструмент)
    spine38_binary_to_json.py  ← парсер 3.8 binary
    spine_batch_converter.py   ← GUI пакетной конвертации
  examples/
    plum.json                  ← binary (чистый)
    center_lt_fx.json          ← binary (чистый)
    plum_readable.json         ← уже сконвертированный пример


ТРЕБОВАНИЯ
----------
• Python 3.9+
• Для GUI: tkinter (входит в python.org / большинство дистрибутивов)
• Опционально: pip install spine_asset  (fallback в GUI)


АВТОРСТВО ПАРСЕРА
-----------------
Формат binary: EsotericSoftware Spine 3.8 SkeletonBinary.
Инструмент: обёртка для batch/CLI восстановления misnamed .json → readable JSON.
