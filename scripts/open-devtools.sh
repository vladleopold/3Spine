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
# выбираем вкладку Resources Saver через командную палитру DevTools
xdotool key --window "$win" --clearmodifiers ctrl+shift+p
sleep 2
xdotool type --window "$win" --delay 60 "Resources Saver"
sleep 2
xdotool key --window "$win" Return
sleep 4
log "вкладка Resources Saver выбрана через командную палитру"

log "DevTools открыт (F12)"

# диагностика: окно DevTools в CDP не видно (Chrome отдаёт его как browser_ui),
# поэтому шаг не считаем ошибкой — кнопку нажмём на странице панели.
PORT="${CDP_PORT:-9222}"
targets="$(curl -fsS --max-time 3 "http://127.0.0.1:$PORT/json/list" 2>/dev/null || true)"
types="$(printf '%s' "$targets" | grep -o '"type"[[:space:]]*:[[:space:]]*"[^"]*"' | sort | uniq -c | tr '\n' ' ')"
log "цели Chrome: ${types:-нет}"
if printf '%s' "$targets" | grep -q 'devtools://'; then
  log "окно DevTools доступно по CDP"
elif printf '%s' "$targets" | grep -q 'browser_ui'; then
  log "DevTools открыт, но его окно видно только как browser_ui — нажатие кнопки пойдёт через страницу панели"
else
  log "DevTools открыт (F12 отправлен)"
fi
exit 0
