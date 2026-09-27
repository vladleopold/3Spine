# Pragmatic Play UHT — CI

Универсальный экстрактор для **любого** `gameSymbol`:

| Артефакт | Источник | Выход |
|----------|----------|--------|
| PNG | `Texture` base64 | `textures/*.png` |
| Spine JSON | `UHTSpine.spineJSON` | `spine/<name>/<name>.json` |
| Atlas | `UIAtlas.spriteList` | `spine/<name>/<name>.atlas` |
| Page PNG | Texture / CDN | `spine/<name>/<name>.png` |

```bash
./find-uht-assets.sh vs20swbonsup
# UHTSpine часто в main_resources023+ → MAIN_MAX=40 по умолчанию
```

Документация для прода: **[CI-PRAGMATIC-UHT.md](./CI-PRAGMATIC-UHT.md)**
