# CI: универсальное извлечение PNG из игр Pragmatic Play (UHT)

## Что ищем

В resource JSON паках движка **UHT** картинки лежат **inline base64**:

```json
{
  "type": "Texture",
  "id": "bc45f1db2b41de74eb4e162eb853f594",
  "isInline": true,
  "data": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA..."
}
```

Это **не** привязка к Spine. Отдельный шаг CI: найти все такие объекты → записать настоящие `.png` / `.jpg`.

Работает на любом `gameSymbol` (vs20olympgate, vs20chestcol, …).

## Правило детекта (для агента / CI)

1. Скачать resource packs:
   ```
   https://demogamesfree.pragmaticplay.net/gs2c/common/v3/games-html5/games/vs/<SYMBOL>/desktop/
     client/resources.json
     game/main_resources000.json … main_resources0NN.json
     game/GUI_resources000.json …
     game/other_resources000.json …
   ```
2. В каждом JSON искать:
   - `"type":"Texture"`
   - `"id":"<32 hex>"`
   - `"data":"data:image/png;base64,..."` или `data:image/jpeg;base64,...`
3. Base64 декодировать → файл `<id>.png` или `<id>.jpg`
4. Лог CI:
   ```
   FOUND N images (PNG/JPG from base64 Texture)
   KEEP: <id>.png (image/png, SIZE bytes, base64 Texture)
   ```

Не искать «Spine-пару» на этом шаге. Не ждать `.skel` / `.atlas` — только Texture → PNG.

## Файлы для прод-CI

| Файл | Назначение |
|------|------------|
| `extract_uht_textures.py` | Парсер: JSON → PNG |
| `find-textures.sh` | Скачать packs по SYMBOL + вызвать парсер |

### Запуск

```bash
# одна игра
./find-textures.sh vs20chestcol

# или вручную после wget/curl паков
python3 extract_uht_textures.py ./resources -o ./textures
```

### GitHub Actions (фрагмент)

```yaml
jobs:
  extract-textures:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        symbol: [vs20olympgate, vs20chestcol]
    steps:
      - uses: actions/checkout@v4
      - name: Extract PNGs
        run: ./find-textures.sh ${{ matrix.symbol }}
        env:
          OUT_DIR: artifacts/${{ matrix.symbol }}
      - uses: actions/upload-artifact@v4
        with:
          name: textures-${{ matrix.symbol }}
          path: artifacts/${{ matrix.symbol }}/textures/
```

### GitLab CI (фрагмент)

```yaml
extract-textures:
  image: python:3.12-slim
  variables:
    SYMBOL: vs20chestcol
  script:
    - apt-get update && apt-get install -y curl
    - ./find-textures.sh $SYMBOL
  artifacts:
    paths:
      - uht-assets/textures/
```

## Проверка на проде

| Игра | Symbol | Ожидание |
|------|--------|----------|
| Gates of Olympus | `vs20olympgate` | десятки PNG из GUI_resources / other_resources / main_resources |
| Sweet Craze | `vs20chestcol` | ~100+ PNG из main_resources* |

Критерий успеха CI: `FOUND N images` где **N > 0**, файлы открываются как PNG (magic `\x89PNG`).

## Чего этот шаг не делает

- Не собирает Spine-пары (json+atlas+png) — для этого `extract_uht_spine.py`
- Не качает внешние `…/res/<hash>.png` (опционально отдельно)
- Не требует session `mgckey` для public demo CDN
