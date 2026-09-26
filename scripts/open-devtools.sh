#!/usr/bin/env bash
# Отдельный шаг: открыть DevTools в уже запущенном Chrome (F12 в окно).
# Работает, потому что Chrome запущен на виртуальном экране (Xvfb), а не в headless.
set -euo pipefail

log() { printf '%s\n' "$*" >&2; }
DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":$DISPLAY_NUM"

if ! command -v xdotool >/dev/null 2>&1; then
  log "ставлю xdotool"
  sudo apt-get update -qq
  sudo apt-get install -y -qq xdotool >/dev/null
fi

# окно Chrome
win=""
for i in $(seq 1 20); do
  win="$(xdotool search --onlyvisible --class 'google-chrome' 2>/dev/null | tail -1 || true)"
  [ -n "$win" ] && break
  win="$(xdotool search --onlyvisible --name 'Chrome' 2>/dev/null | tail -1 || true)"
  [ -n "$win" ] && break
  sleep 1
done
if [ -z "$win" ]; then
  log "не нашёл окно Chrome"
  exit 1
fi
log "окно Chrome: $win"

xdotool windowactivate --sync "$win" 2>/dev/null || xdotool windowfocus "$win" || true
sleep 1
# F12 — открыть/переключить DevTools
xdotool key --window "$win" F12
sleep 4
# на всякий случай второй раз, если окно ещё не в фокусе
xdotool key --window "$win" F12 2>/dev/null || true
sleep 3
log "DevTools открыт (F12)"
