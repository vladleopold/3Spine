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
  Xvfb ":$DISPLAY_NUM" -screen 0 3200x1400x24 -nolisten tcp >/dev/null 2>&1 &
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
  --window-size=3000,1300 --window-position=0,0 \
  --enable-unsafe-extension-debugging \
  --disable-features=DisableLoadExtensionCommandLineSwitch,Translate,OptimizationHints \
  --disable-extensions-except="$EXT_DIR" \
  --load-extension="$EXT_DIR" \
  "$URL_" >/tmp/chrome.log 2>&1 &
echo $! > /tmp/chrome.pid
log "Chrome запущен, pid $(cat /tmp/chrome.pid), DevTools порт $PORT"

# ждём, когда DevTools-порт поднимется
ready=0
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[ "$ready" = 1 ] || { log "Chrome не поднял порт отладки"; cat /tmp/chrome.log >&2 || true; exit 1; }

# Расширение обязано быть установлено ДО открытия DevTools: панель
# chrome.devtools.panels.create регистрируется только при старте окна.
# Chrome 137+ вдобавок игнорирует --load-extension, поэтому ставим через CDP.
EXT_ABS="$(cd "$(dirname "$EXT_DIR")" && pwd)/$(basename "$EXT_DIR")"
EXT_ABS="$EXT_ABS" PORT="$PORT" node -e '
const port = process.env.PORT || 9222;
const p = process.env.EXT_ABS;
(async () => {
  const v = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  const ws = new WebSocket(v.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", () => rej(new Error("WebSocket не подключился")), { once: true });
  });
  let n = 0; const wait = new Map();
  ws.addEventListener("message", (e) => {
    const m = JSON.parse(e.data); const w = wait.get(m.id);
    if (!w) return; wait.delete(m.id);
    m.error ? w.rej(new Error(m.error.message)) : w.res(m.result);
  });
  const send = (method, params) => {
    const id = ++n; ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res, rej) => wait.set(id, { res, rej }));
  };
  try {
    const r = await send("Extensions.loadUnpacked", { path: p });
    console.error("Расширение загружено через CDP: " + ((r && r.id) || "ок"));
  } catch (e) { console.error("Extensions.loadUnpacked: " + e.message); }
})();
' || true

log "Chrome готов"
exit 0
log "Chrome не поднял порт отладки"; cat /tmp/chrome.log >&2 || true; exit 1
