#!/usr/bin/env bash
# CI: universal Pragmatic Play UHT texture (base64 → PNG) extractor
set -euo pipefail

SYMBOL="${1:-vs20olympgate}"
OUT_DIR="${OUT_DIR:-./uht-assets}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTRACTOR="${SCRIPT_DIR}/extract_uht_textures.py"
BASE="https://demogamesfree.pragmaticplay.net/gs2c/common/v3/games-html5/games/vs/${SYMBOL}/desktop"

log() { echo "[find-textures] $*"; }

mkdir -p "${OUT_DIR}/resources"
log "Symbol: ${SYMBOL}"
log "Downloading resource packs…"

download() {
  local rel="$1"
  local dest="${OUT_DIR}/resources/${rel}"
  mkdir -p "$(dirname "$dest")"
  if curl -sL -f -A "Mozilla/5.0 CI-Bot" -o "$dest" "${BASE}/${rel}"; then
    local sz
    sz=$(wc -c < "$dest" | tr -d ' ')
    if [[ "$sz" -gt 200 ]]; then
      log "OK  ${rel} (${sz} bytes)"
      return 0
    fi
  fi
  rm -f "$dest"
  return 1
}

# Standard UHT layout (same across Pragmatic HTML5 slots)
RELS=(
  client/resources.json
  client/game.json
  game/GUI_resources000.json
  game/GUI_resources001.json
  game/other_resources000.json
  game/other_resources001.json
)
for i in $(seq 0 20); do
  RELS+=("game/main_resources$(printf '%03d' "$i").json")
done
for i in $(seq 0 5); do
  RELS+=("game/game$(printf '%03d' "$i").json")
  RELS+=("game/GUI$(printf '%03d' "$i").json")
done

ok=0
for rel in "${RELS[@]}"; do
  download "$rel" && ok=$((ok + 1)) || true
done
log "Downloaded ${ok} resource files"

log "Extracting base64 Textures → PNG…"
python3 "${EXTRACTOR}" "${OUT_DIR}/resources" -o "${OUT_DIR}/textures"

n=$(find "${OUT_DIR}/textures" -type f \( -name '*.png' -o -name '*.jpg' \) 2>/dev/null | wc -l | tr -d ' ')
log "DONE: ${n} images in ${OUT_DIR}/textures/"
[[ "$n" -gt 0 ]]
