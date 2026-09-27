#!/usr/bin/env bash
# Ставит transcoder KTX → PNG, чтобы страницы Spine-атласов были картинками.
# Необязательный шаг: если ничего не поставилось, экстрактор отдаст .ktx как есть.
set -uo pipefail

log() { echo "[ktx-tools] $*"; }

have() { command -v "$1" >/dev/null 2>&1; }

if have ktx2ktx2 || have toktx || have ktx || have basisu; then
  log "transcoder уже есть: $(command -v ktx2ktx2 || command -v toktx || command -v ktx || command -v basisu)"
  exit 0
fi

# 1) KTX-Software из apt (если пакет есть в дистрибутиве)
if have apt-get; then
  log "пробую apt: ktx-tools"
  if sudo apt-get update -qq >/dev/null 2>&1 && \
     sudo apt-get install -y -qq ktx-tools >/dev/null 2>&1 && have ktx2ktx2; then
    log "поставил ktx2ktx2 из apt"
    exit 0
  fi
fi

# 2) Basis Universal: статический бинарь из релизов на GitHub
if have curl; then
  url=$(curl -sL --max-time 30 \
    https://api.github.com/repos/BinomialLLC/basis_universal/releases/latest |
    sed -n 's/.*"browser_download_url": *"\([^"]*linux[^"]*\.zip\)".*/\1/p' | head -1)
  if [ -n "$url" ]; then
    log "качаю basis_universal: ${url##*/}"
    tmp=$(mktemp -d)
    if curl -sL --max-time 120 -o "$tmp/basis.zip" "$url" && \
       command -v unzip >/dev/null 2>&1 && unzip -oq "$tmp/basis.zip" -d "$tmp"; then
      bin=$(find "$tmp" -type f -name 'basisu*' ! -name '*.txt' | head -1)
      if [ -n "$bin" ]; then
        sudo install -m 0755 "$bin" /usr/local/bin/basisu 2>/dev/null || true
        have basisu && { log "поставил basisu"; rm -rf "$tmp"; exit 0; }
      fi
    fi
    rm -rf "$tmp"
  fi
fi

# 3) ImageMagick — последний шанс (KTX поддержан не везде)
if have apt-get; then
  log "пробую ImageMagick"
  sudo apt-get install -y -qq imagemagick >/dev/null 2>&1 || true
fi

if have ktx2ktx2 || have toktx || have ktx || have basisu; then
  log "transcoder готов"
else
  log "transcoder не поставился — .ktx останутся .ktx (данные не теряются)"
fi
exit 0
