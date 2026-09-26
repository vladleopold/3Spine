#!/usr/bin/env bash
# Шаг 1+2: поднимаем виртуальный экран и запускаем Chrome с расширением и ссылкой.
# Chrome остаётся жить между шагами (его PID сохраняем), порт отладки открыт.
set -euo pipefail

log() { printf '%s\n' "$*" >&2; }

URL_="${URL:?нужен URL}"
EXT_DIR="${EXT_DIR:-$PWD/.chrome-ext}"
PROFILE="${PROFILE:-$PWD/.chrome-profile}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
PORT="${CDP_PORT:-9222}"
CHROME_BIN="${CHROME_PATH:-/usr/bin/google-chrome}"

mkdir -p "$PROFILE" artifacts

# 1) виртуальный экран
if ! xdpyinfo -display ":$DISPLAY_NUM" >/dev/null 2>&1; then
  Xvfb ":$DISPLAY_NUM" -screen 0 1600x1000x24 -nolisten tcp >/dev/null 2>&1 &
  echo $! > /tmp/xvfb.pid
  sleep 2
fi
echo "DISPLAY=:$DISPLAY_NUM" >> "$GITHUB_ENV"
log "Xvfb готов на :$DISPLAY_NUM"

# window manager: без него xdotool не может активировать окно (_NET_ACTIVE_WINDOW)
if ! pgrep -x matchbox-window-manager >/dev/null 2>&1; then
  if ! command -v matchbox-window-manager >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq matchbox-window-manager >/dev/null
  fi
  DISPLAY=":$DISPLAY_NUM" matchbox-window-manager -use_titlebar no >/dev/null 2>&1 &
  sleep 1
fi
log "window manager запущен"

# 2) Chrome с расширением и ссылкой
DISPLAY=":$DISPLAY_NUM" "$CHROME_BIN" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-sandbox --disable-dev-shm-usage --no-first-run --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  --window-size=1500,950 --window-position=0,0 \
  --disable-features=DisableLoadExtensionCommandLineSwitch \
  --enable-unsafe-extension-debugging \
  --disable-features=Translate,OptimizationHints \
  --disable-extensions-except="$EXT_DIR" \
  --load-extension="$EXT_DIR" \
  "$URL_" >/tmp/chrome.log 2>&1 &
echo $! > /tmp/chrome.pid
log "Chrome запущен, pid $(cat /tmp/chrome.pid), DevTools порт $PORT"

# ждём, когда DevTools-порт поднимется
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
    log "Chrome готов"
    exit 0
  fi
  sleep 1
done
log "Chrome не поднял порт отладки"; cat /tmp/chrome.log >&2 || true; exit 1
