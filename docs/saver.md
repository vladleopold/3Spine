# Headless-сейвер ресурсов (`scripts/download-resources.mjs`)

Универсальный скрипт, который открывает страницу игры в браузере, снимает **всю**
сеть, раскрывает манифесты и складывает результат в ZIP + отчёт. Аналог
Resources-Saver, но переписанный на Playwright и рассчитанный на CI.

Запуск: `npm run download` (или `node scripts/download-resources.mjs`).

---

## Что делает

### 1. Перехват всей сети страницы

Подписывается на ответы **и** на страницу, **и** на контекст браузера:

- `page.on('response')` — обычные XHR/fetch;
- `ctx.on('response')` — ответы из **web-worker и service-worker** (через `page`
  они не видны, а именно там часто грузятся `wasm` и бинарные манифесты игр);
- `page.on('download')` / `ctx.on('download')` — «настоящие» скачивания, когда
  игра отдаёт файл, а не запрос (кладётся в `_downloads/`);
- финальный **CDP-обход** `Page.getResourceTree` → `Page.getResourceContent` —
  то, чем пользуется Resources-Saver через DevTools: всё, что осталось в кеше
  браузера, обходится рекурсивно по `childFrames` (игра живёт в iframe).

Сохраняются только ответы `200–399` с непустым телом. Расширение Spine-файла
берётся из `pathname` URL (query-строка ломала проверку `/\.(atlas|skel|bin)$/`).

Шум отсекается регуляркой `NOISE`: Cloudflare-челленджи, аналитика, реклама
(googletagmanager, google-analytics, doubleclick, facebook, tiktok, criteo,
360yield, yandex.metrika, hotjar, amplitude, ipify и т. п.). Счётчик отброшенного
доступен как `ignoredNoise`.

### 2. Разбор манифестов

JS/JSON-ответы (< 4 МБ) сразу парсятся функцией `harvest()`:

- пары `"uuid": "images/foo.png"` (формат, которым славятся провайдеры
  PlaysOnSite/Cocos, Kendoo и др.);
- массивы `"path" | "paths" | "file" | "files" | "assets" | "res" |
  "resources": [ ... ]`.

Берутся только известные расширения: `png jpg jpeg webp avif atlas skel bin json
mp3 wav ogg fnt ttf otf woff woff2 wasm plist`. Относительные пути резолвятся
от базового URL. Глубина рекурсии — `MANIFEST_DEPTH` (по умолчанию 2), лимит — 6000
URL на набор. В конце найденное выкачивается **в той же сессии** через
`ctx.request.get` с заголовками `Referer` и `Accept-Language` (до 4000 URL, не
более 1500 успешных).

### 3. Поиск игрового шелла через API

Страница казино — обёртка: сама игра лежит на отдельном хосте. Если после
первого прохода Spine-файлов нет, `resolveGameUrl()` ищет шелл:

1. Из уже пойманной сети и из HTML достаются хосты, похожие на API
   (`api*`, `api-gw`, `games-api`).
2. Запрашивается каталог: `/games/providers/games`, `/api/games/providers/games`
   или `/games`; ответ разворачивается (`games` / `data.games` / `data`).
3. В каталоге ищется запись, совпадающая со слагом (из `game-term`, `game` или
   последнего сегмента пути).
4. Пробуются demo-endpoint'ы: `/games/demo?provider=&term=`,
   `/games/{id}/demo`, `/games/play?...&demo=true`; из ответа берётся
   `url | game_url | launch_url | data.url | game.url`.
5. Найденный шелл открывается **в том же контексте**; если и там нет Spine —
   делается ещё один резолв (до 3 проходов суммарно).

Готовую ссылку можно задать руками через `GAME_URL`.

### 4. Развёрнутая сессия на persistent-профиле

Если задан `PROFILE`, используется `chromium.launchPersistentContext(...)`:
один профиль на все проходы, поэтому куки, `localStorage`, кеш и «память» игры
живут между страницей-обёрткой и игровым шеллом. Обычный режим — временный
контекст (`chromium.launch` + `newContext`).

Общее для обоих режимов:

- UA: Windows Chrome 129, `locale: uk-UA`, `Accept-Language: uk-UA,uk;q=0.9,en;q=0.8`,
  viewport 1920×1080, `ignoreHTTPSErrors`;
- флаги: `--no-sandbox --disable-web-security --disable-gpu
  --disable-blink-features=AutomationControlled` и др.;
- `navigator.webdriver` принудительно `undefined` (Cloudflare / fingerprint-чеки);
- проход по двум барьерам: age-gate (18+) и кнопка запуска игры. Сначала
  специфичные селекторы площадки, потом общий список (`Play`, `Demo`, `Играть`,
  `Демо`, `Грати`, `Открыть игру`, `iframe canvas`…). Клик идёт только если
  элемент виден и его bounding box ≥ 120×60 — иначе это не кнопка;
- ожидание догрузки короткими циклами до `WAIT_MS` (без одного долгого
  `sleep`).

### 5. Структура по хостам, ZIP и отчёт

Путь файла восстанавливается из URL: `<OUTPUT_DIR>/<безопасное-имя-url>/<hostname>/<path>`.
Символы `<>:"|?*` и control-символы заменяются на `_`, директория с `/` →
`index.html`. Если в URL есть query — к имени добавляется 10-символьный
base64url-хеш query, чтобы `foo.atlas?x=1` и `foo.atlas?x=2` не сливались.

Итог по каждому URL:

- `downloaded/<safe-name>/<hostname>/…` — сами ресурсы;
- `downloaded/<safe-name>/_downloads/…` — файловые скачивания;
- `downloaded/<safe-name>/saver-report.json` — `{ url, files, spine, manifestUrls }`;
- `downloaded/<safe-name>.zip` — архив каталога (создаётся, если есть что архивировать).

`spine` считается не как `min(atlas, skel)`, а как число **пар**
`<имя>.atlas` + `<имя>.skel|.bin` в одном каталоге (суффикс query-хеша перед
сравнением снимается функцией `stripQuerySuffix`).

---

## Быстрый старт

```bash
npm ci
npx playwright install chromium
URLS="https://first.ua/ua/igrovie-avtomaty/kendoo/4-gold-carts" npm run download
```

Без `URLS` берётся встроенный список из 7 игр (см. матрицу CI ниже).

**macOS 13.** На этой системе `npx playwright install chromium` не ставит
браузер (несовместимая сборка), поэтому скрипт подхватывает **системный
Chrome**. Он ищется в таком порядке: `$CHROME_PATH` →
`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` →
`/usr/bin/google-chrome` → `/usr/bin/chromium` → `/usr/bin/chromium-browser`.
Если ничего не найдено — используется браузер Playwright.

Открыть с окном (для отладки, видно, что происходит на странице):

```bash
HEADLESS=false PROFILE=./.chrome-profile npm run download
```

---

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `URLS` | список из 7 игр | URL через запятую. Пусто = дефолтный список |
| `OUTPUT_DIR` | `./downloaded` | Куда складываются каталоги, ZIP и отчёты (в CI очищается) |
| `WAIT_MS` | `35000` | Доп. ожидание догрузки после открытия страницы |
| `HEADLESS` | `true` | `false` — видимый браузер |
| `PROFILE` | — | Путь к persistent-профилю; включает `launchPersistentContext` |
| `PROXY` | — | `http://user:pass@host:port` (работает в обоих режимах) |
| `GAME_URL` | — | Уже известный игровой шелл, если авто-резолв не нужен |
| `MANIFEST_DEPTH` | `2` | Глубина разбора манифестов |
| `CHROME_PATH` | — | Явный путь к системному Chrome/Chromium |

---

## Запуск в CI

Workflow: **Download Slot Resources** —
`.github/workflows/download-assets.yml`. Триггер — ручной `workflow_dispatch`
с входами:

- `urls` — URL через запятую (пусто = дефолтные 7);
- `wait_ms` — доп. ожидание после загрузки, по умолчанию `35000`;
- `full` — `true`: прогнать все URL в одной job вместо матрицы.

Job `tests` (gate для всего): checkout → Node 20 c `cache: npm` → `npm ci` →
`npm test` (`node --test test/` — юнит-тесты логики сейвера: пути, манифесты,
Spine-пары).

Job `download` (`needs: tests`, `fail-fast: false`, timeout 60 мин) — матрица
из 7 игр, по job на игру:

| Имя в матрице | URL |
|---|---|
| `first-ua-kendoo` | `https://first.ua/ua/igrovie-avtomaty/kendoo/4-gold-carts` |
| `slotcity-spinjoy` | `https://slotcity.ua/?modals=game&game-term=spinjoy-meduzas-fortune&demo=true` |
| `cosmolot-demonic-dolls` | `https://cosmolot.ua/ua/game/demonic-dolls` |
| `playson-clover-strike` | `https://playson.com/game/clover-strike-hold-and-win` |
| `3oaks-superpower-diamonds` | `https://3oaks.com/game/3_superpower_diamonds` |
| `slotor777-demo` | `https://slotor777.ua/ru/game/view/56dab5f9954f459f919d800306e48b35?mode=demo` |
| `beton-dog-house` | `https://beton.ua/game/pragmaticplay-direct-the-dog-house-megaways-1000?isDemo=true` |

Шаги: checkout → Node 20 → `npm ci` → `npx playwright install chromium --with-deps`
→ `node scripts/download-resources.mjs` с `URLS` (свой список при `full=true`,
иначе `matrix.url`), `OUTPUT_DIR=./artifacts`, `WAIT_MS`, `HEADLESS=true`.

Далее `if: always()`:

- **Отчёт по Spine** — `find` считает файлы, `*.atlas`, `*.skel`, `*.bin`,
  картинки (`png/jpg/webp/avif`), и строка
  `файлов: N | atlas: A | skel: K | bin: B | картинки: I` уходит в
  `$GITHUB_STEP_SUMMARY` (то есть видно прямо на странице запуска);
- **upload-artifact** — имя `slot-<имя>-<run_id>`, путь `artifacts`,
  `retention-days: 14`, `if-no-files-found: warn`.

---

## Как понять, что всё скачалось

1. **В логе есть `★SPINE`.** Метка печатается только для `.atlas`, `.skel`,
   `.bin` и только целиком, без обрезки URL:
   ```
     ✓ ★SPINE   184.2 KB  https://cdn.../game.atlas
   ```
   Если `★SPINE` нет — Spine не нашлись, дальше по цепочке идти нечего.
2. **Есть пара `atlas` + `skel` (или `bin`).** Итоговая строка по каждому URL:
   `Итого по <safe>: N файлов (Spine: M)` — `M` это число пар, а не число
   файлов. Ноль при наличии atlas'ов означает, что скачался только atlas без
   скелеона (или наоборот) — пара не сошлась.
3. **`saver-report.json`** рядом с каталогом: `files`, `spine`, `manifestUrls`.
4. **`images/<атлас>/`.** Рядом с `.atlas` должна лежать папка `images/` с
   текстурами (`png`/`webp`/`jpg`) — atlas без картинок конвертировать нечего.
   В `GITHUB_STEP_SUMMARY` это видно как счётчик «картинок».
5. ZIP-архив в корне `OUTPUT_DIR` — `<safe-name>.zip`; в артефактах CI он
   лежит рядом с каталогом ресурсов.

---

## Ограничения и что делать

| Площадка | Проблема | Что делать |
|---|---|---|
| `playson.com`, `cosmolot.ua` | Cloudflare Turnstile: вместо игры отдаётся челлендж, в логе `⚠ ни одной кнопки не нажал` и нет `★SPINE` | Нужен реальный браузер с прокси резидентного региона: `PROXY=... PROFILE=./.profile HEADLESS=false`. Прокси и профиль задаются **вместе** — прокси работает только в persistent-режиме и в обычном контексте. Один раз пройти челлендж вручную в том же профиле |
| `first.ua` | Гео-блок: приходит `restriction.*` (страница «доступ ограничен»), ресурсы не грузятся | Прокси под регион игры + `Accept-Language: uk-UA` (уже встроен). Проверять, что в артефактах нет каталогов `restriction` — это не ресурсы игры |
| Все площадки | Кнопка запуска не нажалась: игра не стартовала, сети нет | У кнопок есть per-site селекторы в карте `SITE` (`text=Демо`, `[class*="launch"]`, `.modal button`, `[class*="play-btn"]`, `[class*="game-btn"]`, `[class*="btn-play"]`, `text=Play` …) — если вёрстка площадки поменялась, правьте `scripts/download-resources.mjs:319-327`. Отлаживать с `HEADLESS=false` |
| Отдельная ссылка | API-резолв не сработал (хост не похож на `api*`, нет demo-endpoint) | Задать `GAME_URL` руками — скрипт пойдёт туда в той же сессии |
| Медленный CDN | Не всё успело догрузиться | Поднять `WAIT_MS`; дождаться `networkidle` в цикле |
| Ручной запуск с macOS 13 | Playwright не поставил Chromium | Задать `CHROME_PATH` к системному Chrome |

---

## Карта файлов

| Файл | Роль |
|---|---|
| `scripts/download-resources.mjs` | сам сейвер: перехват сети, `harvest()` манифестов, `resolveGameUrl()`, клики по барьерам, CDP-обход, `countSpine()`, ZIP, `saver-report.json` |
| `test/saver.test.mjs` | тесты логики сейвера (`npm test`) |
| `test/core.test.mjs` | остальные тесты пакета |
| `.github/workflows/download-assets.yml` | **Download Slot Resources**: gate `npm test` + матрица из 7 URL + step summary + артефакты |
| `.github/workflows/saver-selftest.yml` | самотест сейвера |
| `package.json` | скрипты `download` и `test`, зависимости `playwright`, `fs-extra`, `archiver` |
