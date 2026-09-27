#!/usr/bin/env bash
# =============================================================================
# CI: Find Spine data inside Pragmatic Play Gates of Olympus (vs20olympgate)
# =============================================================================
# Usage:
#   ./find-spine.sh                  # full run
#   ./find-spine.sh --download-only  # only download resources
#   ./find-spine.sh --scan-only      # scan already downloaded files
#
# Exit codes:
#   0  - Spine signatures found
#   1  - no Spine signatures found
#   2  - download / network error
#   3  - invalid arguments
# =============================================================================
set -euo pipefail

SYMBOL="${SYMBOL:-vs20olympgate}"     # переопределяется из CI для любой игры
BASE="https://demogamesfree.pragmaticplay.net"
GAME_PATH="/gs2c/common/v3/games-html5/games/vs/${SYMBOL}/desktop"
OUT_DIR="${OUT_DIR:-./olympus-assets}"
REPORT="${OUT_DIR}/spine-report.txt"
MANIFEST="${OUT_DIR}/urls.txt"

MODE="full"
case "${1:-}" in
  --download-only) MODE="download" ;;
  --scan-only)     MODE="scan" ;;
  --help|-h)
    sed -n '2,20p' "$0"
    exit 0
    ;;
  "") ;;
  *)
    echo "Unknown arg: $1" >&2
    exit 3
    ;;
esac

mkdir -p "${OUT_DIR}/game" "${OUT_DIR}/client" "${OUT_DIR}/logs"
exec > >(tee -a "${OUT_DIR}/logs/run.log") 2>&1

log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die()  { log "ERROR: $*"; exit 2; }

# -----------------------------------------------------------------------------
# 1. Bootstrap: openGame → get session + resource keys
# -----------------------------------------------------------------------------
download_resources() {
  log "=== Download phase ==="
  local open_url="${BASE}/gs2c/openGame.do?gameSymbol=${SYMBOL}&websiteUrl=${BASE}&jurisdiction=99&lang=en&cur=EUR"

  log "Opening demo session..."
  # Follow redirects, capture final HTML + effective URL
  local html
  html=$(curl -sL -A "Mozilla/5.0 CI-Bot" \
    -c "${OUT_DIR}/cookies.txt" \
    -w "\n%{url_effective}" \
    --max-time 30 \
    "${open_url}") || die "openGame.do failed"

  local final_url
  final_url=$(echo "${html}" | tail -n1)
  html=$(echo "${html}" | sed '$d')
  log "Final URL: ${final_url}"

  # Extract mgckey if present
  local mgckey
  mgckey=$(echo "${final_url}" | grep -oP 'mgckey=[^&]+' | cut -d= -f2- || true)
  log "Session key: ${mgckey:0:40}..."

  # Discover resource URLs from bootstrap / network-like patterns
  # We hardcode known resource names + fetch with ?key= from build if possible
  local resources=(
    "client/resources.json"
    "client/game.json"
    "game/GUI000.json"
    "game/GUI001.json"
    "game/GUI002.json"
    "game/GUI003.json"
    "game/GUI004.json"
    "game/GUI005.json"
    "game/GUI006.json"
    "game/GUI_resources000.json"
    "game/GUI_resources001.json"
    "game/game000.json"
    "game/game001.json"
    "game/game002.json"
    "game/game003.json"
    "game/game004.json"
    "game/main_resources000.json"
    "game/main_resources001.json"
    "game/main_resources002.json"
    "game/main_resources003.json"
    "game/main_resources004.json"
    "game/main_resources005.json"
    "game/main_resources006.json"
    "game/main_resources007.json"
    "game/main_resources008.json"
    "game/main_resources009.json"
    "game/main_resources010.json"
    "game/main_resources011.json"
    "game/main_resources012.json"
    "game/main_resources013.json"
    "game/main_resources014.json"
    "game/main_resources015.json"
    "game/main_resources016.json"
    "game/other_resources000.json"
    "game/other_resources001.json"
    "build.js"
    "bootstrap.js"
    "style.css"
  )

  : > "${MANIFEST}"
  local ok=0 fail=0

  for rel in "${resources[@]}"; do
    local url="${BASE}${GAME_PATH}/${rel}"
    local out="${OUT_DIR}/${rel}"
    mkdir -p "$(dirname "${out}")"

    # Try without key first (many work), then with empty key param
    if curl -sL -A "Mozilla/5.0 CI-Bot" \
         --max-time 60 \
         -o "${out}" \
         -w "%{http_code}" \
         "${url}" | grep -qE '^(200|304)$'; then
      local size
      size=$(wc -c < "${out}" | tr -d ' ')
      if [[ "${size}" -gt 100 ]]; then
        log "OK  ${rel} (${size} bytes)"
        echo "${url}" >> "${MANIFEST}"
        ok=$((ok + 1))
        continue
      fi
    fi

    # Fallback: try with ?key= (some CDNs require any key)
    if curl -sL -A "Mozilla/5.0 CI-Bot" \
         --max-time 60 \
         -o "${out}" \
         -w "%{http_code}" \
         "${url}?key=ci" | grep -qE '^(200|304)$'; then
      local size
      size=$(wc -c < "${out}" | tr -d ' ')
      if [[ "${size}" -gt 100 ]]; then
        log "OK  ${rel}?key=ci (${size} bytes)"
        echo "${url}?key=ci" >> "${MANIFEST}"
        ok=$((ok + 1))
        continue
      fi
    fi

    log "FAIL ${rel}"
    rm -f "${out}"
    fail=$((fail + 1))
  done

  log "Downloaded: ${ok} ok, ${fail} failed"
  [[ "${ok}" -gt 0 ]] || die "No resources downloaded"
}

# -----------------------------------------------------------------------------
# 2. Unpack UHT-embedded Spine (NOT classic .skel files)
# -----------------------------------------------------------------------------
# Pragmatic UHT packs:
#   type:"UHTSpine"  → data.spineJSON = base64(classic Spine JSON)
#   type:"Texture"   → data = data:image/png;base64,...
#   UIAtlas          → spriteList rects (not classic .atlas text)
# -----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTRACTOR="${SCRIPT_DIR}/extract_uht_spine.py"

scan_spine() {
  log "=== Unpack UHT Spine phase ==="
  : > "${REPORT}"

  {
    echo "UHT Spine unpack report — $(date -u -Iseconds)"
    echo "Symbol: ${SYMBOL}"
    echo "========================================"
    echo ""
    echo "FORMAT NOTES (for agent / human):"
    echo "  • Standalone .skel / .atlas files do NOT exist in this build."
    echo "  • Skeletons live inside main_resources*.json as:"
    echo '      { "type":"UHTSpine", "id":"<guid>", "data":{'
    echo '          "name":"..._SkeletonData",'
    echo '          "spineJSON":"<base64 classic Spine JSON>" } }'
    echo "  • Textures: { \"type\":\"Texture\", \"data\":\"data:image/png;base64,...\" }"
    echo "  • Atlases: UIAtlas.spriteList { x,y,width,height } + texture GUID"
    echo "    (NGUI-style, not Spine atlas text — rebuild .atlas if needed)"
    echo "  • How to detect: grep '\"type\":\"UHTSpine\"' + decode spineJSON"
    echo "  • How to unpack: extract_uht_spine.py (base64 → skeletons/*.json)"
    echo "========================================"
  } >> "${REPORT}"

  if [[ ! -f "${EXTRACTOR}" ]]; then
    log "ERROR: extractor not found at ${EXTRACTOR}"
    return 1
  fi

  local extract_out="${OUT_DIR}/extracted"
  mkdir -p "${extract_out}"

  # Prefer main_resources / other_resources / game* — where UHTSpine lives
  local inputs=()
  while IFS= read -r -d '' f; do
    inputs+=("$f")
  done < <(find "${OUT_DIR}" -type f -name '*.json' \( \
      -name 'main_resources*.json' -o \
      -name 'other_resources*.json' -o \
      -name 'game*.json' -o \
      -name 'GUI*.json' -o \
      -name 'resources*.json' \
    \) -print0 2>/dev/null)

  if [[ ${#inputs[@]} -eq 0 ]]; then
    log "No resource JSON found under ${OUT_DIR}"
    echo "No resource JSON found" >> "${REPORT}"
    return 1
  fi

  log "Running extractor on ${#inputs[@]} files (skeletons + PNG)..."
  set +e
  python3 "${EXTRACTOR}" "${inputs[@]}" -o "${extract_out}" --fetch-cdn 2>&1 | tee -a "${REPORT}"
  local rc=${PIPESTATUS[0]}
  set -e

  local n_proj=0 n_pair=0
  local proj_dir="${extract_out}/spine_projects"
  if [[ -d "${proj_dir}" ]]; then
    n_proj=$(find "${proj_dir}" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
    # complete pair = has .json + .atlas + (.png|.jpg)
    while IFS= read -r d; do
      local base
      base=$(basename "$d")
      if [[ -f "$d/${base}.json" && -f "$d/${base}.atlas" ]] && \
         { [[ -f "$d/${base}.png" ]] || [[ -f "$d/${base}.jpg" ]]; }; then
        n_pair=$((n_pair + 1))
      fi
    done < <(find "${proj_dir}" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
  fi

  {
    echo ""
    echo "========================================"
    echo "Spine projects : ${n_proj}"
    echo "Complete pairs : ${n_pair}  (json + atlas + png)"
    echo "Each project = Spine pair in spine_projects/<name>/"
    if [[ "${n_proj}" -gt 0 ]]; then
      echo "PROJECTS:"
      find "${proj_dir}" -mindepth 1 -maxdepth 1 -type d -printf '  %f\n' 2>/dev/null | sort
    fi
  } >> "${REPORT}"

  log "Spine projects: ${n_proj}  |  complete pairs (json+atlas+png): ${n_pair}"
  log "Report: ${REPORT}"
  log "Projects dir: ${proj_dir}/"

  if [[ "${n_proj}" -gt 0 ]]; then
    log "FOUND ${n_proj} Spine projects (${n_pair} complete pairs)"
    while IFS= read -r d; do
      local base
      base=$(basename "$d")
      for f in "$d"/*; do
        [[ -f "$f" ]] || continue
        local kind sz
        sz=$(wc -c < "$f" | tr -d ' ')
        case "$f" in
          *.json)  kind="Spine skeleton" ;;
          *.atlas) kind="Spine atlas" ;;
          *.png|*.jpg) kind="Spine page PNG" ;;
          *) kind="file" ;;
        esac
        log "KEEP: spine_projects/${base}/$(basename "$f") (${kind}, ${sz} bytes)"
      done
    done < <(find "${proj_dir}" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)
    return 0
  fi

  log "No Spine projects found"
  return 1
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
log "OUT_DIR=${OUT_DIR}  MODE=${MODE}"

case "${MODE}" in
  download)
    download_resources
    log "Done (download-only)"
    exit 0
    ;;
  scan)
    scan_spine && exit 0 || exit 1
    ;;
  full)
    download_resources
    if scan_spine; then
      log "SUCCESS — Spine signatures located"
      cat "${REPORT}"
      exit 0
    else
      log "No clear Spine signatures found (see report)"
      cat "${REPORT}" || true
      exit 1
    fi
    ;;
esac
