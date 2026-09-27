#!/usr/bin/env bash
# Ставит transcoder KTX → PNG, чтобы страницы Spine-атласов были картинками.
# Необязательный шаг: если ничего не поставилось, экстрактор отдаст .ktx как есть.
set -uo pipefail

log() { echo "[ktx-tools] $*"; }

have() { command -v "$1" >/dev/null 2>&1; }

works() { command -v "$1" >/dev/null 2>&1 && "$1" --help >/dev/null 2>&1; }
for t in ktx2ktx2 toktx ktx basisu; do
  if works "$t"; then
    log "transcoder уже есть: $t ($(command -v "$t"))"
    exit 0
  fi
done

# 1) KTX-Software из apt (если пакет есть в дистрибутиве)
if have apt-get; then
  log "пробую apt: ktx-tools"
  if sudo apt-get update -qq >/dev/null 2>&1 && \
     sudo apt-get install -y -qq ktx-tools >/dev/null 2>&1 && have ktx2ktx2; then
    log "поставил ktx2ktx2 из apt"
    exit 0
  fi
fi

# 2) KTX-Software от Khronos: готовые Linux-сборки, компилировать не нужно.
#    Кладём дерево в /opt и делаем обёртки с LD_LIBRARY_PATH — так надёжнее,
#    чем полагаться на ldconfig.
if have curl && have tar; then
  log "качаю KTX-Software (готовые Linux-бинары)"
  url=$(curl -sL --max-time 30 https://api.github.com/repos/KhronosGroup/KTX-Software/releases/latest |
        sed -n 's/.*"browser_download_url": *"\([^"]*Linux-x86_64\.tar\.bz2\)".*/\1/p' | head -1)
  if [ -n "$url" ]; then
    tmp=$(mktemp -d)
    if curl -sL --max-time 180 -o "$tmp/ktx.tar.bz2" "$url" && \
       tar -xjf "$tmp/ktx.tar.bz2" -C "$tmp" 2>/dev/null; then
      root=$(find "$tmp" -maxdepth 1 -mindepth 1 -type d | head -1)
      if [ -n "$root" ] && [ -d "$root/lib" ]; then
        sudo rm -rf /opt/ktx-software
        sudo mkdir -p /opt/ktx-software
        sudo cp -R "$root/lib" /opt/ktx-software/ 2>/dev/null || true
        sudo mkdir -p /opt/ktx-software/bin
        for b in ktx2ktx2 toktx ktx ktxinfo ktx2check; do
          [ -f "$root/bin/$b" ] || continue
          sudo cp "$root/bin/$b" /opt/ktx-software/bin/
          sudo chmod 0755 /opt/ktx-software/bin/"$b"
          printf '#!/bin/sh\nLD_LIBRARY_PATH=/opt/ktx-software/lib exec /opt/ktx-software/bin/%s "$@"\n' "$b" \
            | sudo tee /usr/local/bin/"$b" >/dev/null
          sudo chmod 0755 /usr/local/bin/"$b"
        done
        if ktx2ktx2 --help >/dev/null 2>&1; then
          log "поставил ktx2ktx2 из KTX-Software (в /opt/ktx-software)"
          rm -rf "$tmp"
          exit 0
        fi
        log "бинарь не запускается, пробую ldconfig"
        sudo install -m 0755 "$root"/lib/libktx.so* /usr/local/lib/ 2>/dev/null || true
        sudo ldconfig 2>/dev/null || true
        if ktx2ktx2 --help >/dev/null 2>&1; then
          log "починил через ldconfig"
          rm -rf "$tmp"
          exit 0
        fi
      fi
    fi
    rm -rf "$tmp"
  fi
fi

# 3) Basis Universal: статический бинарь из релизов на GitHub
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

# 4) ImageMagick — последний шанс (KTX поддержан не везде)
if have apt-get; then
  log "пробую ImageMagick"
  sudo apt-get install -y -qq imagemagick >/dev/null 2>&1 || true
fi

# 5) Сборка basisu из исходников. Готовых бинарей в релиз��х нет, поэтому
#    только по запросу: занимает несколько минут.
if [ "${KTX_BUILD_FROM_SOURCE:-0}" = "1" ] && ! have basisu; then
  if ! have git || ! have cmake || ! have g++; then
    log "ставлю сборочные зависимости (git, cmake, g++)"
    sudo apt-get update -qq >/dev/null 2>&1 || true
    sudo apt-get install -y -qq git cmake g++ >/dev/null 2>&1 || true
  fi
  if have git && have cmake && have g++; then
    log "собираю basisu из исходников (KTX_BUILD_FROM_SOURCE=1)"
    tmp=$(mktemp -d)
    if git clone --depth 1 -q https://github.com/BinomialLLC/basis_universal.git "$tmp/basis"; then
      log "клон ok, конфигурирую cmake"
      # Один поток и без SSE/примеров: на 2-ядерном раннере тяжёлая сборка
      # C++ с -j 2 упирается в память и падает где-то на середине.
      ( cd "$tmp/basis" && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
          -DBASISU_SSE=OFF -DBASISU_OPENCL=OFF -DBASISU_EXAMPLES=OFF \
          -DBASISU_TESTS=OFF 2>&1 | tail -n 12 )
      ( cd "$tmp/basis" && cmake --build build --target basisu -j 1 2>&1 | tail -n 30 )
      bin=$(find "$tmp/basis/build" -type f -name 'basisu' -perm -u+x ! -name '*.txt' 2>/dev/null | head -1)
      if [ -z "$bin" ]; then
        log "бинарь не найден, ищем собранные объекты:"
        find "$tmp/basis/build" -type f -name 'basisu*' 2>/dev/null | head -n 5
      fi
      if [ -n "$bin" ]; then
        sudo install -m 0755 "$bin" /usr/local/bin/basisu 2>/dev/null || true
        have basisu && { log "собрал basisu из исходников"; rm -rf "$tmp"; exit 0; }
      fi
    fi
    rm -rf "$tmp"
  else
    log "нужны git и cmake для сборки basisu"
  fi
fi

if have ktx2ktx2 || have toktx || have ktx || have basisu; then
  log "transcoder готов"
else
  log "transcoder не поставился — .ktx останутся .ktx (данные не теряются)"
fi
exit 0
