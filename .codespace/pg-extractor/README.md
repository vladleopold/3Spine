# CI: Spine project pairs из Gates of Olympus (UHT)

## Выход CI — пары Spine, не россыпь файлов

```
extracted/spine_projects/
  gool_zeus_transition/
    gool_zeus_transition.json    ← skeleton (из UHTSpine.spineJSON base64)
    gool_zeus_transition.atlas   ← classic Spine atlas (из UIAtlas.spriteList)
    gool_zeus_transition.png     ← page image
  gool_hv_1/
    gool_hv_1.json
    gool_hv_1.atlas
    gool_hv_1.png
  …
```

Каждая папка = **одна Spine-пара**. CI пишет:

```
FOUND 42 Spine projects (N complete pairs)
KEEP: spine_projects/gool_zeus_transition/gool_zeus_transition.json (Spine skeleton, …)
KEEP: spine_projects/gool_zeus_transition/gool_zeus_transition.atlas (Spine atlas, …)
KEEP: spine_projects/gool_zeus_transition/gool_zeus_transition.png (Spine page PNG, …)
```

## Откуда берётся

| Файл пары | Источник в UHT |
|-----------|----------------|
| `.json` | `"type":"UHTSpine"` → `data.spineJSON` (base64) |
| `.atlas` | `UIAtlas.spriteList` + texture GUID → classic atlas text |
| `.png` | `"type":"Texture"` inline base64 **или** CDN `…/res/<guid>.png` |

Связка skeleton↔atlas: компонент `SpineController` (`spineData.guid` + `spineAtlases[].guid`).

## Запуск

```bash
./find-spine.sh
# или
python3 extract_uht_spine.py path/to/resources/*.json -o out/ --fetch-cdn
```

Импорт в Spine Editor: открыть папку проекта, Import Data → `.json`, atlas+png рядом.

---

# devbook

Готово. Кратко, что изменилось и как это работает.
Формат UHT (то, что нужно «разъяснить агенту»)
Отдельных .skel / .atlas нет. Всё внутри resource JSON:






























ТипПолеСодержимое"type":"UHTSpine"data.spineJSONbase64 → обычный Spine JSON (bones, slots, skins, animations)"type":"Texture"datadata:image/png;base64,... — спрайтшитUIAtlasspriteList{x,y,width,height,rotate} + GUID текстуры (не classic .atlas)SpineController в сценеspineData.guidссылка на UHTSpine по GUID
Версия скелетов в Olympus: Spine 3.7.91.
Как определять
Bashgrep -c '"type":"UHTSpine"' main_resources*.json   # скелеты
grep -c 'data:image/png;base64' main_resources*.json  # текстуры
grep -c '"componentType":"UIAtlas"' game*.json       # мета атласов
Как распаковать
Добавлен extract_uht_spine.py — это и есть «обратный процесс» (публичной утилиты UHT от Pragmatic нет).
Bashpython3 extract_uht_spine.py main_resources*.json -o extracted/
На реальных файлах уже вытаскивает 42 скелета, например:
textKEEP: gool_zeus_transition_SkeletonData.json (Spine skeleton, UHT-unpacked)
KEEP: gool_bigwin_SkeletonData.json (Spine skeleton, UHT-unpacked)
FOUND 42 Spine skeletons (UHT unpacked)
CI
find-spine.sh теперь:

Качает resource-паки
Гоняет extract_uht_spine.py
Пишет лог «FOUND N Spine skeletons» вместо «не Spine-скелет»
Кладёт готовые JSON в extracted/skeletons/

Структура:
textolympus-ci/
├── find-spine.sh
├── extract_uht_spine.py    ← распаковщик UHT
├── README.md               ← описание формата для агента
├── .github/workflows/...
└── .gitlab-ci.ymlа спрайты /images тоже нужно выкачать и взять их как картинки пнгRequest was interrupted by the user.Interruptedя говорю что CI должна уметь вытаскивать картинки и прямо сразу брать их как png научи или пропиши это в ciГотово. CI теперь вытаскивает картинки и сохраняет их как настоящие PNG/JPG.
Что делает экстрактор

Inline из JSON ("type":"Texture" + data:image/png;base64,...) → файлы в extracted/textures/*.png
CDN sprite sheets (.../game/res/<hash>.png) → те же большие атлас-листы
В логе CI:

textFOUND 30 images as real PNG/JPG
KEEP: b7a45c001e18b9f41b59da0652632f32.png (image PNG/JPG, 503320 bytes)
KEEP: 3237267f46a865641ac884e77a2aca49.png (image PNG/JPG, 993571 bytes)
...
Запуск
Bash./find-spine.sh
# или только распаковка:
python3 extract_uht_spine.py olympus-assets/game/*.json -o extracted/ --fetch-cdn
Результат:
textextracted/
├── skeletons/     ← Spine JSON
├── textures/      ← реальные .png / .jpg  ← вот это
└── atlases_meta/  ← spriteList (координаты на шитах)
Картинки уже валидные PNG (можно открыть в любом просмотрщике), не base64 и не «обёртки».spineJSON должны браться отдельно как spine-проекты, и атласы от каждого spineJSON должны браться как отдельные файлы и они должны пониматься CI как  spine пара (быть отдельными файлами).Готово. CI теперь отдаёт Spine-пары, а не россыпь файлов.
Структура выхода
textextracted/spine_projects/
  gool_zeus_transition/
    gool_zeus_transition.json    ← skeleton
    gool_zeus_transition.atlas   ← classic atlas
    gool_zeus_transition.png     ← page image
  gool_hv_1/
    gool_hv_1.json
    gool_hv_1.atlas
    gool_hv_1.png
  …
Каждая папка = один Spine-проект (пара skeleton + atlas + png).
Лог CI
textFOUND 42 Spine projects (N complete pairs)
KEEP: spine_projects/gool_zeus_transition/gool_zeus_transition.json (Spine skeleton, …)
KEEP: spine_projects/gool_zeus_transition/gool_zeus_transition.atlas (Spine atlas, …)
KEEP: spine_projects/gool_zeus_transition/gool_zeus_transition.png (Spine page PNG, …)
Как собирается пара





















ФайлИсточник.jsonUHTSpine.spineJSON (base64 → обычный Spine JSON).atlasUIAtlas.spriteList → классический atlas text.pnginline Texture или CDN res/<guid>.png
Связка через SpineController: spineData.guid ↔ spineAtlases[].guid.
