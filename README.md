# Spine Converter — web GUI + Linux-конвертер в Docker (v1.0)

Конвертация Spine-скелетов (`.skel` → читаемый JSON) **настоящим**
`SpineSkeletonDataConverter` (C++, Linux ELF), который исполняется **внутри
Linux-контейнера (Docker)**. Локально Linux не нужен.

```
┌──────────────────────┐    upload ZIP (.skel + images)     ┌────────────────────────────────────────┐
│  GitHub Pages (GUI)  │ ──────────────────────────────────▶ │  Linux-контейнер (any Docker host)      │
│  выбор папки, Start, │ ◀────────────────────────────────── │  ├─ backend/server.py   (HTTP :8080)    │
│  лог, скачивание     │     result ZIP (JSON + images)      │  ├─ backend/converter/*  (SpineC++ ELF)│
└──────────────────────┘                                      │  └─ GitHub Actions (Linux + Docker)   │
                                                             └────────────────────────────────────────┘
```

- **Frontend** (`frontend/`) — статика на GitHub Pages. GUI сам находит сервер.
- **Backend** (`backend/`) — `server.py` (чистый Python stdlib). Принимает ZIP,
  запускает нативный `SpineSkeletonDataConverter` на каждый `.skel`, отдаёт
  результат (JSON + изображения).
- **Docker** — `backend/Dockerfile` + `docker-compose.yml`:
  `backend` (сервис :8080) и `runner` (GitHub Actions, Linux + Docker).
- Конвертер — Linux ELF, работает **только** в контейнере. Python-парсер не используется.

## Репозиторий

```text
vladleopold/3Spine   (новый, отдельный — spine-link не затрагивается)
```

## Как это работает: сайт → GitHub Actions

GUI на Pages ничего не хранит на Mac и не зависит от него. Конвертация:

1. Браузер (сессия) подключается к GitHub API — токен вставляется один раз
   и хранится только в памяти/сессии браузера (или по желанию в localStorage).
2. Пользователь выбирает папку → сайт сам собирает ZIP и кладёт его через
   Git API на ветку `inbox` репозитория.
3. Workflow `web-convert.yml` (Linux, `ubuntu-latest`) превращает `input.zip`
   в `output.zip` настоящим C++-конвертером и пушит результат на ветку
   `results`.
4. Сайт забирает `output.zip` с ветки `results` и отдаёт пользователю.

Репо-секрет `ACCESS_TOKEN` (только для CI, в браузер не попадает) и
`roles/permissions` workflow настроены. Опционально есть ручной поток через
Codespaces: `.devcontainer/` + `codespace-convert.yml` (поднять → сконвертить →
остановить).

## Quick start

Локально ничего устанавливать не нужно. Вся работа идёт на Linux прямо
в GitHub Actions (у него уже есть Docker):

- CI/CD `converter-test.yml` (push в `main`): собирает образ `ubuntu:24.04`,
  гоняет настоящий C++-конвертер на реальных `.skel` в контейнере, проверяет
  HTTP-сервис и сквозной E2E — всё на GitHub-hosted Linux.
- GUI: https://vladleopold.github.io/3Spine/ (адрес сервера определяется
  автоматически как `127.0.0.1:8080`/`localhost:8080` и не требует ввода).

## Установка на целевой машине

### Способ А — Docker (рекомендуется)

Нужен только Docker (на Linux / macOS / Windows — любая ОС, внутри Linux):

```bash
cp .env.example .env   # ACCESS_TOKEN
docker compose up -d --build
curl http://127.0.0.1:8080/health   # → {"status":"ok","converter":true}
```

Скрипты: `setup/up.sh`, `setup/down.sh`.

### Способ Б — без Docker (native Linux)

```bash
sudo ./setup/install_server.sh    # systemd :8080
./setup/install_runner.sh vladleopold/3Spine <REG_TOKEN>
```

## GitHub Pages

Pages включается workflow `.github/workflows/pages.yml` (push в `main`).
Использование:
1. Откройте https://vladleopold.github.io/3Spine/ .
2. Индикатор «✓ сервер онлайн» загорится автоматически, когда backend-контейнер
   с :8080 доступен с той же машины/сети.
3. **Выбрать папку…** → **Start** → лог → **Скачать результат**.

## Эталонные примеры (examples/)

Настоящие `.skel` (3.8) из SpineBatchApp: `plum.skel`, `Light.skel`,
`center_lt_fx.skel`, `seven_light.skel` + эталонные `_converted.json` /
`_readable.json`. Их использует CI (`converter-test.yml`): конвертер обязан
дать валидный JSON со спиной 3.8.x для `plum.skel` (5 костей, 4 слота,
1 анимация), а также прогоняет остальные файлы и печатает статусы.

## Безопасность

- Сервис на `0.0.0.0:8080` без аутентификации — рекомендация: закрыть порт
  firewall'ом (доступ только из сети пользователя) или за reverse-proxy + Basic Auth.
- Лимит загрузки 500 МБ, результаты живут 24 ч.
- CORS — только `localhost`, `127.0.0.1` и `https://vladleopold.github.io`.
- Включён Private Network Access (`Access-Control-Allow-Private-Network: true`),
  чтобы https-Pages мог работать с локальным backend.