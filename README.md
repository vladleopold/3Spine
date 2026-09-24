# Spine Converter — web GUI + Linux-конвертер в Docker (v1.0)

Конвертация Spine-скелетов (`.skel` → читаемый JSON) **настоящим**
`SpineSkeletonDataConverter` (C++, Linux ELF), который исполняется **внутри
Linux-контейнера (Docker)**. Локально Linux не нужен.

```
┌──────────────────────┐    upload ZIP (.skel + images)     ┌────────────────────────────────────────┐
│  GitHub Pages (GUI)  │ ──────────────────────────────────▶ │  Linux-контейнер (any Docker host)      │
│  выбор папки, Start, │ ◀────────────────────────────────── │  ├─ backend/server.py   (HTTP :8080)    │
│  лог, скачивание     │     result ZIP (JSON + images)      │  ├─ backend/converter/*  (SpineC++ ELF)│
└──────────────────────┘                                      │  └─ GitHub Actions self-hosted runner    │
                                                            └────────────────────────────────────────┘
```

- **Frontend** (`frontend/`) — статика на GitHub Pages. GUI сам находит сервер.
- **Backend** (`backend/`) — `server.py` (чистый Python stdlib). Принимает ZIP,
  запускает нативный `SpineSkeletonDataConverter` на каждый `.skel`, отдаёт
  результат (JSON + изображения).
- **Docker** — `backend/Dockerfile` + `docker-compose.yml`:
  `backend` (сервис :8080) и `runner` (self-hosted runner, Linux).
- Конвертер — Linux ELF, работает **только** в контейнере. Python-парсер не используется.

## Репозиторий

```text
vladleopold/3Spine   (новый, отдельный — spine-link не затрагивается)
```

## Quick start (двумя командами, на любой машине с Docker)

```bash
git clone git@github.com:vladleopold/3Spine.git && cd 3Spine
cp .env.example .env          # впишите ACCESS_TOKEN (repo scope)
docker compose up -d --build  # поднимет backend :8080 + self-hosted runner
```

- GUI: https://vladleopold.github.io/3Spine/ (ничего не вводить — адрес сервера
  определяется автоматически как `127.0.0.1:8080`/`localhost:8080`).
- Runner регистрируется автоматически (VS Code / compose) и выполняет
  `.github/workflows/converter-test.yml` на Linux прямо в контейнере.

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

## Self-hosted runner из Visual Studio / VS Code

1. Откройте репозиторий в VS Code (+ расширение GitHub Actions).
2. `docker compose up -d runner` (или запустите образ `ghcr.io/myoung34/github-runner`
   с переменными из `.env`) — контейнер сам зарегистрирует runner
   `spine-linux-x64` с метками `self-hosted, linux, X64`.
3. Runner появится в repo → Settings → Actions → Runners, после чего
   `converter-test.yml` (job `self-hosted-runner`) выполняется на нём.

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