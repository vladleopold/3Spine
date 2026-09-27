#!/usr/bin/env bash
# CI: Pragmatic Play UHT — download packs + extract PNG / Spine JSON / atlas
#
# Usage: ./find-uht-assets.sh <gameSymbol>
# Env:
#   OUT_DIR       default ./uht-assets/<symbol>
#   MAIN_MAX      default 50  (UHTSpine often in 023+ / 036+)
#   GAME_MAX      default 12
#   GUI_RES_MAX   default 8
#   PLATFORMS     default "desktop mobile"
#                 (some games put UHTSpine only under mobile/, e.g. vswaysdragden)
set -euo pipefail

SYMBOL="${1:?Usage: $0 <gameSymbol>  e.g. vswaysdragden}"
OUT_DIR="${OUT_DIR:-./uht-assets/${SYMBOL}}"
MAIN_MAX="${MAIN_MAX:-50}"
GAME_MAX="${GAME_MAX:-12}"
GUI_RES_MAX="${GUI_RES_MAX:-8}"
PLATFORMS="${PLATFORMS:-desktop mobile}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTRACTOR="${SCRIPT_DIR}/extract_pragmatic_uht.py"
ROOT="https://demogamesfree.pragmaticplay.net/gs2c/common/v3/games-html5/games/vs/${SYMBOL}"

log() { echo "[uht-ci] $*"; }

mkdir -p "${OUT_DIR}/resources"
log "Symbol=${SYMBOL}  platforms=${PLATFORMS}  main=0..${MAIN_MAX}"

download() {
  local platform="$1" rel="$2"
  local dest="${OUT_DIR}/resources/${platform}/${rel}"
  mkdir -p "$(dirname "$dest")"
  if [[ -f "$dest" ]] && [[ $(wc -c <"$dest" | tr -d ' ') -gt 200 ]]; then
    return 0
  fi
  if curl -sL -f -A "Mozilla/5.0 CI-Bot" --connect-timeout 15 --max-time 90 \
       -o "$dest" "${ROOT}/${platform}/${rel}"; then
    local sz; sz=$(wc -c <"$dest" | tr -d ' ')
    if [[ "$sz" -gt 200 ]]; then
      log "GET ${platform}/${rel} (${sz})"
      return 0
    fi
  fi
  rm -f "$dest"
  return 1
}

ok=0
fail=0
for platform in $PLATFORMS; do
  for rel in client/resources.json client/game.json; do
    download "$platform" "$rel" && ok=$((ok+1)) || fail=$((fail+1))
  done
  i=0
  while [[ $i -le $GAME_MAX ]]; do
    ii=$(printf '%03d' "$i")
    download "$platform" "game/game${ii}.json" && ok=$((ok+1)) || fail=$((fail+1))
    download "$platform" "game/GUI${ii}.json" && ok=$((ok+1)) || fail=$((fail+1))
    i=$((i+1))
  done
  i=0
  while [[ $i -le $MAIN_MAX ]]; do
    ii=$(printf '%03d' "$i")
    download "$platform" "game/main_resources${ii}.json" && ok=$((ok+1)) || fail=$((fail+1))
    i=$((i+1))
  done
  i=0
  while [[ $i -le $GUI_RES_MAX ]]; do
    ii=$(printf '%03d' "$i")
    download "$platform" "game/GUI_resources${ii}.json" && ok=$((ok+1)) || fail=$((fail+1))
    download "$platform" "game/other_resources${ii}.json" && ok=$((ok+1)) || fail=$((fail+1))
    i=$((i+1))
  done
done
log "Downloaded ok=${ok} missing/empty=${fail}"

if [[ "$ok" -lt 3 ]]; then
  log "ERROR: too few packs"
  exit 1
fi

log "Pre-scan UHTSpine…"
while IFS= read -r f; do
  if grep -q 'UHTSpine' "$f" 2>/dev/null && grep -q 'spineJSON' "$f" 2>/dev/null; then
    log "  spineJSON in ${f#${OUT_DIR}/resources/}"
  fi
done < <(find "${OUT_DIR}/resources" -name '*.json' -size +200c)

python3 "${EXTRACTOR}" "${OUT_DIR}/resources" -o "${OUT_DIR}/extracted" --symbol "${SYMBOL}"

n_tex=$(find "${OUT_DIR}/extracted/textures" -type f \( -name '*.png' -o -name '*.jpg' \) 2>/dev/null | wc -l | tr -d ' ')
n_spine=$(find "${OUT_DIR}/extracted/spine" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
n_pair=0
if [[ -d "${OUT_DIR}/extracted/spine" ]]; then
  while IFS= read -r d; do
    b=$(basename "$d")
    if [[ -f "$d/${b}.json" && -f "$d/${b}.atlas" ]] && \
       { [[ -f "$d/${b}.png" ]] || [[ -f "$d/${b}.jpg" ]]; }; then
      n_pair=$((n_pair+1))
    fi
  done < <(find "${OUT_DIR}/extracted/spine" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
fi
log "DONE textures=${n_tex} spine_projects=${n_spine} complete_pairs=${n_pair}"

if [[ "$n_tex" -eq 0 && "$n_spine" -eq 0 ]]; then
  log "ERROR: nothing extracted"
  exit 1
fi
if [[ "$n_spine" -eq 0 ]]; then
  log "WARNING: no UHTSpine (try MAIN_MAX=60 or PLATFORMS='desktop mobile')"
fi
exit 0
