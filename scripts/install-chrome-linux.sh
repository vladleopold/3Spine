#!/usr/bin/env bash
# Установка Google Chrome в Linux (Ubuntu/Debian) — нужен для headless-захвата
# страниц расширением в GitHub Ubuntu CI. Идемпотентно: если Chrome уже есть,
# скрипт просто печатает версию и выходит.
set -euo pipefail

log() { printf '%s\n' "$*" >&2; }

if command -v google-chrome >/dev/null 2>&1; then
  log "Chrome уже установлен: $(google-chrome --version)"
  exit 0
fi

if [ -x /usr/bin/google-chrome-stable ]; then
  log "Chrome уже установлен: $(/usr/bin/google-chrome-stable --version)"
  exit 0
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    log "Нужен root или sudo для установки Chrome"
    exit 1
  fi
fi

log "Ставлю Google Chrome…"

# 1) официальный репозиторий Google
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq curl ca-certificates gnupg >/dev/null
curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
  | $SUDO gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
  | $SUDO tee /etc/apt/sources.list.d/google-chrome.list >/dev/null
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq google-chrome-stable >/dev/null

# 2) шрифты и локаль — без них кириллица в ZIP ломается
$SUDO apt-get install -y -qq fonts-liberation fonts-dejavu-core fonts-noto-core >/dev/null || true
$SUDO locale-gen ru_RU.UTF-8 >/dev/null 2>&1 || true

log "Готово: $(google-chrome --version)"
