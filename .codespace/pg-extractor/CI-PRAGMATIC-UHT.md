# CI: универсальное обнаружение PNG / Atlas / Spine JSON — Pragmatic Play (UHT)

## Запуск (прод)

```bash
./find-uht-assets.sh vs20olympgate
./find-uht-assets.sh vs20swbonsup
./find-uht-assets.sh vs20chestcol
./find-uht-assets.sh vs10luckybnz

# если spine_json=0 — поднять потолок packs:
MAIN_MAX=50 ./find-uht-assets.sh vs20swbonsup
```

Выход:

```
uht-assets/<symbol>/extracted/
  textures/*.png          ← все base64 Texture
  spine/<name>/
    <name>.json           ← Spine skeleton (из UHTSpine.spineJSON)
    <name>.atlas          ← classic atlas (из UIAtlas), если есть
    <name>.png            ← page image
  extract-report.txt
```

---

## Правила детекта

### 1. PNG / sprites

```json
{ "type":"Texture", "id":"<32-hex>", "isInline":true,
  "data":"data:image/png;base64,..." }
```

→ `textures/<id>.png`  
Лог: `KEEP: textures/….png (PNG/JPG, N bytes, base64 Texture)`

### 2. Spine skeleton (JSON, не .skel)

```json
{ "type":"UHTSpine", "id":"<guid>",
  "data": { "name":"xxx_SkeletonData", "spineJSON":"<base64>", "scale":0.01 } }
```

→ decode base64 → `spine/<name>/<name>.json`  
Версии обычно Spine **3.7.91** / **3.7.9.1**.  
**Binary `.skel` в public demo packs нет.**

### 3. Atlas + page PNG

- `UIAtlas.spriteList` → `.atlas` text  
- `textureContent.guid` → Texture PNG (inline или CDN `…/res/<guid>.png`)  
- Связка: `SpineController.spineData.guid` ↔ `spineAtlases[].guid`

### 4. Где лежат packs

```
https://demogamesfree.pragmaticplay.net/gs2c/common/v3/games-html5/games/vs/<SYMBOL>/desktop/
  game/main_resources000.json … main_resources0NN.json   ← Texture + часто UHTSpine
  game/game000.json …                                    ← SpineController, UIAtlas
  game/GUI_resources*.json                               ← UI + иногда Spine
  game/other_resources*.json
  client/resources.json
```


### Desktop vs Mobile

Некоторые игры кладут **игровые UHTSpine только в `mobile/`** (пример: `vswaysdragden` / Dragon Pots Megaways):

| Platform | UHTSpine |
|----------|----------|
| desktop  | часто только UI (lobby, volatility) |
| mobile   | `main_resources036+` — basegame, symbols, bigwin… |

CI по умолчанию качает **оба** (`PLATFORMS="desktop mobile"`, `MAIN_MAX=50`).

**Важно:** `UHTSpine` часто в **высоких** индексах (`main_resources023`, `024`, …), не только в 000–010.  
CI по умолчанию качает **main_resources 0..40** (`MAIN_MAX`).

Пример (Sweet Bonanza Super Scatter):

| Pack | Содержимое |
|------|------------|
| main_resources023 | 16× UHTSpine (символы, scatter, multiplier…) |
| main_resources024 | 5× UHTSpine (basegame, bigwin, freegame…) |
| GUI_resources001 | lobby_button, volatility_indicator |

---

## Файлы CI

| Файл | Роль |
|------|------|
| `find-uht-assets.sh` | Скачать packs (до MAIN_MAX) + pre-scan + extract |
| `extract_pragmatic_uht.py` | Детект Texture / UHTSpine / UIAtlas → файлы |

---

## Лог (эталон)

```
[uht-ci] UHTSpine in main_resources023.json
[uht-ci] UHTSpine in main_resources024.json
=== 1/3 TEXTURES ===
KEEP: textures/….png …
=== 2/3 SPINE JSON ===
Detected: UHTSpine=22  UIAtlas=136  SpineController links=…
=== 3/3 SPINE PROJECTS ===
KEEP: spine/sbss_super_scatter/sbss_super_scatter.json …
KEEP: spine/sbss_super_scatter/sbss_super_scatter.atlas …
KEEP: spine/sbss_super_scatter/sbss_super_scatter.png …

FOUND textures=131 spine_json=22 pairs=20
```

---

## GitHub Actions

```yaml
name: pragmatic-uht
on:
  workflow_dispatch:
  schedule: [{ cron: "0 6 * * *" }]
jobs:
  extract:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        symbol: [vs20olympgate, vs20chestcol, vs20swbonsup, vs10luckybnz]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - name: Extract
        run: MAIN_MAX=40 ./find-uht-assets.sh ${{ matrix.symbol }}
        env:
          OUT_DIR: artifacts/${{ matrix.symbol }}
      - uses: actions/upload-artifact@v4
        with:
          name: uht-${{ matrix.symbol }}
          path: |
            artifacts/${{ matrix.symbol }}/extracted/textures/
            artifacts/${{ matrix.symbol }}/extracted/spine/
            artifacts/${{ matrix.symbol }}/extracted/extract-report.txt
```

## GitLab CI

```yaml
extract-uht:
  image: python:3.12-slim
  parallel:
    matrix:
      - SYMBOL: [vs20olympgate, vs20chestcol, vs20swbonsup, vs10luckybnz]
  variables:
    MAIN_MAX: "40"
  before_script:
    - apt-get update && apt-get install -y curl
  script:
    - OUT_DIR=artifacts/$SYMBOL ./find-uht-assets.sh $SYMBOL
  artifacts:
    paths: [artifacts/$SYMBOL/extracted/]
    when: always
```

---

## Критерии успеха

| Уровень | Условие |
|---------|---------|
| Pass | `textures >= 1` **или** `spine_json >= 1` |
| Full Spine | `spine_json >= 1` и желательно `pairs > 0` |
| Warning | `spine_json=0` при `MAIN_MAX` — в логе WARNING, job не падает если есть PNG |

Проверки:
- PNG: magic `\x89PNG`
- Skeleton: JSON с `skeleton` / `bones`
- Atlas: текстовый формат Spine (`size:`, `xy:`, `rotate:`)
