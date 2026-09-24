# Spine Converter — web GUI + native Linux converter (v1.0)

Конвертация Spine-скелетов (`.skel` → читаемый JSON) **настоящим**
`SpineSkeletonDataConverter` (C++, работает на Linux), с GUI в браузере.

## Как это устроено

```
┌─────────────────┐   загрузка ZIP (.skel + картинки)
│  GitHub Pages   │ ────────────────────────────────▶  ┌─────────────────────────────┐
│  (frontend GUI) │  ◀────────────────────────────────  │ Linux-машина (self-hosted  │
│  выбор папки,   │   результат ZIP (JSON + images)    │ runner + HTTP service :8080)│
│  Start, лог,    │                                    │  backend/server.py           │
│  скачивание     │                                    │  ├─ backend/converter/       │
└─────────────────┘                                    │  │  └─ SpineSkeletonDataConverter  │
                                                       └─────────────────────────────┘
```

- **Frontend** (`frontend/`) — статика на GitHub Pages: выбор папки (webkitdirectory),
  кнопка **Start**, лог, скачивание результата. JSZip встроен, CDN не нужен.
- **Backend** (`backend/`) — HTTP-сервис на чистом Python stdlib (без pip-зависимостей).
  Принимает ZIP, распаковывает, для каждого `.skel` запускает настоящий
  `SpineSkeletonDataConverter`, собирает результат (JSON + сопутствующие изображения)
  обратно в ZIP.
- **Конвертер** — `backend/converter/SpineSkeletonDataConverter` — нативный Linux ELF,
  непреобразованный Python-парсер **не используется**.

## 1. Git репозиторий

Создайте **НОВЫЙ** GitHub-репозиторий (например `spine-converter`) и запушьте проект.

```bash
# локально (в этой папке) уже создаётся git-репозиторий
git init
git add .
git commit -m "Spine Converter v1.0 — web GUI + native Linux converter"
git remote add origin git@github.com:YOUR_USER/spine-converter.git
git push -u origin main
```

## 2. GitHub Pages

1. Репозиторий → **Settings → Pages** → Source: **GitHub Actions**.
2. Деплой происходит по workflow `.github/workflows/pages.yml` (при пуше в `main`).
3. GUI будет доступен по: `https://YOUR_USER.github.io/spine-converter/`

## 3. Linux-машина (раннер + сервис)

На Linux-сервере (внешний IP или VPN) с git + python3 + sudo:

```bash
sudo apt-get update && sudo apt-get install -y git python3 curl jq
```

### 3.1 Self-hosted runner

1. Репозиторий GitHub → **Settings → Actions → Runners → New self-hosted runner** —
   скопируйте **registration token**.
2. Запустите:
```bash
./setup/install_runner.sh YOUR_USER/spine-converter <TOKEN>
```
Раннер зарегистрирован как `self-hosted / linux / X64` (нужен для
`.github/workflows/converter-test.yml`).

### 3.2 Backend-сервис

```bash
sudo ./setup/install_server.sh
sudo ufw allow 8080/tcp   # при необходимости
```

Проверка:
```bash
curl http://<IP LINUX МАШИНЫ>:8080/health
# → {"status":"ok","converter":true,"version":"1.0"}
```

## 4. Использование

1. Откройте GUI: `https://YOUR_USER.github.io/spine-converter/`
2. В правом верхнем углу введите адрес сервера: `http://<IP>:8080` → **Сохранить**
   (индикатор станет зелёным `✓ сервер онлайн`).
3. **Выбрать папку…** — укажите папку со Spine-файлами.
4. **Start** — файлы упакуются, уйдут на сервер, конвертируются настоящим конвертером.
5. В логе — результат по каждому файлу; **Скачать результат** — забрать ZIP.

## Структура репозитория

```
SpineConverter/
├── frontend/          # GitHub Pages GUI (index.html, app.js, style.css, JSZip)
├── backend/
│   ├── server.py      # HTTP-сервис (stdlib): /api/convert, /results/<token>.zip, /health
│   ├── converter.py   # запуск SpineSkeletonDataConverter по папке
│   └── converter/     # нативный Linux-конвертер (ELF)
│       └── SpineSkeletonDataConverter
├── setup/
│   ├── install_runner.sh   # установка self-hosted runner
│   └── install_server.sh   # установка systemd-сервиса :8080
├── test/sample38.json      # фикстура для smoke-теста
└── .github/workflows/
    ├── pages.yml           # деплой frontend на Pages
    └── converter-test.yml  # smoke-тест конвертера (self-hosted runner)
```

## Безопасность

- Сервис привязан к `0.0.0.0:8080` без аутентификации — это приватный инструмент.
  Рекомендуется закрыть порт firewall'ом с доступом только для офиса/VPN, либо выставить
  сервис за reverse-proxy (nginx/caddy) с Basic Auth.
- Лимит загрузки: **500 МБ** (`SPINE_MAX_BYTES`), результаты хранятся 24 ч.
- CORS открыт только для `localhost` и `https://<ваш-user>.github.io`
  (задаётся в `ALLOWED_ORIGINS` в `backend/server.py`).