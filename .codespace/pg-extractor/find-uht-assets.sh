#!/usr/bin/env bash
# CI: Pragmatic Play UHT — download packs + extract PNG / Spine JSON / atlas
#
# Usage: ./find-uht-assets.sh <gameSymbol>
# Env:
#   OUT_DIR       default ./uht-assets/<symbol>
#   SKIP_NA       default 3   (столько подряд 404 подряд → серия кончилась)
#   HARD_MAX      default 200 (потолок перебора, страховка от бесконечного цикла)
#   PLATFORMS     default "desktop mobile"
#                 (some games put UHTSpine only under mobile/, e.g. vswaysdragden)
set -euo pipefail

SYMBOL="${1:?Usage: $0 <gameSymbol>  e.g. vswaysdragden}"
OUT_DIR="${OUT_DIR:-./uht-assets/${SYMBOL}}"
PLATFORMS="${PLATFORMS:-desktop mobile}"
SKIP_NA="${SKIP_NA:-3}"
HARD_MAX="${HARD_MAX:-200}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTRACTOR="${SCRIPT_DIR}/extract_pragmatic_uht.py"
GAMES_ROOT="https://demogamesfree.pragmaticplay.net/gs2c/common/v3/games-html5/games"
ROOT="${GAMES_ROOT}/${FOLDER:-vs}/${SYMBOL}"

# У каждой игры своя папка games/<FOLDER>/<SYMBOL> (у слотов это vs, но символы
# бывают с другим префиксом). Раньше «vs» был зашит, и игра с другим префиксом
# молча давала 0 ассетов. Теперь определяем папку сами, пробуя варианты.
pick_root() {
  local cand url code
  local -a cands=("${FOLDER:-}" "${SYMBOL:0:2}" "vs" "${SYMBOL}")
  for cand in "${cands[@]}"; do
    [[ -z "$cand" ]] && continue
    url="${GAMES_ROOT}/${cand}/${SYMBOL}/desktop/client/game.json"
    code=$(curl -sL -o /dev/null -w '%{http_code}' -A "Mozilla/5.0 CI-Bot" \
                 --connect-timeout 15 --max-time 60 "$url" || echo 000)
    log "проба папки ${cand}: HTTP ${code}"
    if [[ "$code" == "200" ]]; then
      ROOT="${GAMES_ROOT}/${cand}/${SYMBOL}"
      log "папка игры: ${cand}"
      return 0
    fi
  done
  return 1
}

log() { echo "[uht-ci] $*"; }

mkdir -p "${OUT_DIR}/resources"
log "Symbol=${SYMBOL}  platforms=${PLATFORMS}  серии идут до ${SKIP_NA} подряд 404 (потолок ${HARD_MAX})"

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
if ! pick_root; then
  log "не нашёл папку игры ни для одного варианта: ${SYMBOL}"
  exit 1
fi
# Число пакетов у каждой игры своё (у vs20wraanu — 71 main_resources, у других
# бывает 40, а бывает и 120). Раньше стоял жёсткий *_MAX, и у игр с бо́льшим
# числом пакетов часть ассетов просто не докачивалась. Теперь перебираем, пока
# идут файлы, и останавливаемся после SKIP_NA подряд идущих 404.
SKIP_NA="${SKIP_NA:-3}"
HARD_MAX="${HARD_MAX:-200}"

# Скачивает game/<stem><NNN>.json по возрастанию, пока файлы не кончатся.
pull_series() {
  local platform="$1" stem="$2" limit="$3"
  local miss=0 i=0
  while [[ $i -le $limit ]]; do
    local ii; ii=$(printf '%03d' "$i")
    if download "$platform" "game/${stem}${ii}.json"; then
      ok=$((ok+1)); miss=0
    else
      fail=$((fail+1)); miss=$((miss+1))
      [[ $miss -ge $SKIP_NA ]] && break
    fi
    i=$((i+1))
  done
}

fetch_platform() {
  local platform="$1"
  for rel in client/resources.json client/game.json; do
    download "$platform" "$rel" && ok=$((ok+1)) || fail=$((fail+1))
  done
  pull_series "$platform" game      "$HARD_MAX"
  pull_series "$platform" GUI       "$HARD_MAX"
  pull_series "$platform" main_resources  "$HARD_MAX"
  pull_series "$platform" GUI_resources   "$HARD_MAX"
  pull_series "$platform" other_resources "$HARD_MAX"
}

# Сколько скачанных пакетов содержат скелеты.
count_spine_packs() {
  local n=0 f
  while IFS= read -r f; do
    if grep -q 'UHTSpine' "$f" 2>/dev/null && grep -q 'spineJSON' "$f" 2>/dev/null; then
      n=$((n+1))
    fi
  done < <(find "${OUT_DIR}/resources" -name '*.json' -size +200c 2>/dev/null)
  printf '%s' "$n"
}

# Платформы перебираем по очереди и берём первую, где вообще есть скелеты:
# у части игр пакеты лежат только под mobile/ (например vswaysdragden), и
# жёстко прописанная в workflow платформа молча давала 0 скелетов.
# Если скелетов нет — повторяем проход глубже (SKIP_NA=6): серия могла
# прерваться на пропуске, и докачиваем только недостающее (download кэширует).
tried_deep=0
for platform in $PLATFORMS; do
  fetch_platform "$platform"
  n_spine_packs=$(count_spine_packs)
  if [[ "$n_spine_packs" -eq 0 && "$tried_deep" -eq 0 ]]; then
    log "платформа ${platform}: spineJSON не найдено — повторяю глубже (SKIP_NA=6)"
    saved_skip="$SKIP_NA"; SKIP_NA=6; tried_deep=1
    fetch_platform "$platform"
    SKIP_NA="$saved_skip"
    n_spine_packs=$(count_spine_packs)
  fi
  log "платформа ${platform}: пакетов со spineJSON=${n_spine_packs} (всего ok=${ok} miss=${fail})"
  [[ "$n_spine_packs" -gt 0 ]] && break
  log "платформа ${platform}: скелетов нет, пробую следующую"
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
  log "WARNING: no UHTSpine (попробуй SKIP_NA=6 или PLATFORMS='desktop mobile')"
fi
exit 0
