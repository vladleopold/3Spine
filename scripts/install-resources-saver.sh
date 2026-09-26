#!/usr/bin/env bash
# Установка расширения Resources-Saver (Alex313031/Resources-Saver) в Chrome.
# Каталог расширения кладём в EXT_DIR, чтобы actions/cache мог переиспользовать
# его между прогонами. Идемпотентно: если уже скачано — просто печатает версию.
set -euo pipefail

log() { printf '%s\n' "$*" >&2; }

REPO="${EXT_REPO:-Alex313031/Resources-Saver}"
EXT_DIR="${EXT_DIR:-$PWD/.chrome-ext}"

if [ -f "$EXT_DIR/manifest.json" ]; then
  log "Расширение уже скачано: $EXT_DIR"
else
  log "Скачиваю $REPO…"
  tmp="$(mktemp -d)"
  # main/master — на случай смены ветки по умолчанию
  ok=0
  for br in main master; do
    if curl -fsSL --max-time 60 \
        "https://codeload.github.com/$REPO/tar.gz/refs/heads/$br" \
        -o "$tmp/repo.tgz"; then ok=1; break; fi
  done
  if [ "$ok" -ne 1 ]; then
    log "не удалось скачать $REPO (ветки main и master недоступны)"
    exit 1
  fi
  tar -xzf "$tmp/repo.tgz" -C "$tmp"
  root="$(find "$tmp" -maxdepth 4 -name manifest.json -print 2>/dev/null | awk '{ print gsub("/","/"), FILENAME }' -F/ 2>/dev/null | sort -n | head -1 | cut -d" " -f2)"
  [ -n "$root" ] || root="$(find "$tmp" -maxdepth 4 -name manifest.json | head -1)"
  if [ -z "$root" ]; then
    log "в архиве нет manifest.json — это не расширение Chrome"
    exit 1
  fi
  rm -rf "$EXT_DIR"
  mkdir -p "$(dirname "$EXT_DIR")"
  mv "$(dirname "$root")" "$EXT_DIR"
  rm -rf "$tmp"
  log "Расширение установлено: $EXT_DIR"
fi

# что именно установили
node -e '
const m = require(process.argv[1] + "/manifest.json");
console.log("name: " + m.name + " v" + m.version + " (manifest v" + m.manifest_version + ")");
console.log("permissions: " + (m.permissions || []).join(", "));
' "$EXT_DIR"
