#!/usr/bin/env bash
# Публикация файлов в ветку results без скачивания её истории.
#
# Обычный приём в workflow выглядит так:
#     git init && git fetch --depth 1 origin results && git checkout -B results FETCH_HEAD
#     … git add … && git commit && git push
# и стоит 3–5 минут: --depth 1 без фильтра тянет ВСЕ блобы ветки, а там
# копятся все архивы прошлых прогонов (за полтора месяца — 2.5 ГБ, 99 zip-ов).
# Мы кладём в results только новые файлы, поэтому старую историю видеть не нужно.
#
# Что делает скрипт:
#   1) git fetch --filter=blob:none  — приходят только коммиты и деревья,
#      ни байта файлов (проверено: 0.8 с против 5 мин);
#   2) git read-tree <дерево>        — индекс собирается по именам, без чтения блобов;
#   3) новые файлы кладём в индекс через hash-object/update-index;
#   4) commit-tree + push            — уходит ровно один новый блоб и один коммит.
#
# Использование:
#   publish-results.sh "сообщение" src1:history/имя.zip src2:history/index.json
# Пути src — файлы рабочей копии (абсолютные или до смены каталога),
# назначения — пути внутри ветки results.
#
# Переменная EXTRACT="путь-в-results:имя-локального-файла[,…]" — перед
# коммитом достать из ветки results конкретные файлы (обычно history/index.json,
# который надо переписать, а не затереть). Git сам докачает только этот блоб:
# git show с partial clone тянет ровно один объект, а не всю ветку.
set -euo pipefail

MSG="${1:?Usage: publish-results.sh <commit-message> <src:dst>...}"
shift
# Файлов может не быть — тогда скрипт годится только для чтения (EXTRACT),
# и коммитить нечего: выходим после подготовки, лишнего коммита не делаем.
[ "$#" -ge 1 ] || [ -n "${EXTRACT:-}" ] || {
  echo "Usage: publish-results.sh <commit-message> <src:dst>... (или только EXTRACT)" >&2
  exit 2
}

REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY не задан}"
TOKEN="${GITHUB_TOKEN:?GITHUB_TOKEN не задан}"

# Исходники читаем до смены каталога: дальше мы внутри временного репозитория.
# Счётчик, а не длина массива: на bash 3.2 (macOS) ${#ABS[@]} на пустом
# массиве под set -u роняет скрипт.
ABS=()
NABS=0
for pair in "$@"; do
  src="${pair%%:*}"
  dst="${pair#*:}"
  [ -f "$src" ] || { echo "::warning::publish-results: нет файла $src — пропускаю" >&2; continue; }
  # git hash-object не понимает относительные пути после смены каталога
  ABS+=("$(cd "$(dirname "$src")" && pwd)/$(basename "$src"):$dst")
  NABS=$((NABS + 1))
done
NEED_PUSH=0
[ "$NABS" -ge 1 ] || echo "::warning::publish-results: файлов к публикации нет" >&2
[ "$NABS" -ge 1 ] && NEED_PUSH=1

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git init -q "$WORK"
cd "$WORK"
git config user.name "3Spine-bot"
git config user.email "3Spine-bot@users.noreply.github.com"
# Без этого push файла крупнее пары мегабайт обрывается на HTTP 400.
git config http.postBuffer 524288000
git config http.version HTTP/1.1
git remote add origin "https://x-access-token:${TOKEN}@github.com/${REPO}.git"

# Пустая ветка (первый запуск в новом репозитории) — тогда индекса нет и
# read-tree заменяем на --empty, чтобы не тянуть оставшиеся файлы.
BASE=""
if git fetch -q --depth 1 --filter=blob:none origin results; then
  BASE="$(git rev-parse FETCH_HEAD)"
  git read-tree "${BASE}^{tree}"
else
  git read-tree --empty
fi

# Файлы, которые нужно ПРОЧИТАТЬ из results перед коммитом (например
# history/index.json — его переписывают, а не затирают).
if [ -n "${EXTRACT:-}" ]; then
  IFS=',' read -r -a _ex <<< "$EXTRACT"
  for item in "${_ex[@]}"; do
    [ -n "$item" ] || continue
    rpath="${item%%:*}"
    lname="${item#*:}"
    if [ "$lname" = "$item" ]; then lname="$(mktemp)"; fi
    if git show "${BASE}:${rpath}" > "$lname" 2>/dev/null; then
      echo "publish-results: прочитан $rpath → $lname"
    else
      echo "::warning::publish-results: в results нет $rpath — считаем пустым" >&2
      : > "$lname"
    fi
  done
fi

# Перебираем только при NABS > 0: на bash 3.2 (macOS) разворачивание пустого
# массива под set -u — ошибка «unbound variable».
if [ "$NABS" -ge 1 ]; then
  for pair in "${ABS[@]}"; do
    src="${pair%%:*}"
    dst="${pair#*:}"
    blob="$(git hash-object -w -- "$src")"
    git update-index --add --cacheinfo "100644,$blob,$dst"
  done
fi

[ "$NEED_PUSH" = "1" ] || { echo "publish-results: только чтение, коммита нет"; exit 0; }

# --missing-ok обязателен: блобов прошлых прогонов у нас нет (их отфильтровал
# blob:none), и обычный write-tree идёт за ними в сеть — 2 минуты на проверку.
# Само дерево от этого не меняется: недостающие блобы остаются нетронутыми.
TREE="$(git write-tree --missing-ok)"
if [ -n "$BASE" ]; then
  COMMIT="$(git commit-tree "$TREE" -p "$BASE" -m "$MSG")"
else
  COMMIT="$(git commit-tree "$TREE" -m "$MSG")"
fi

git push -q origin "${COMMIT}:refs/heads/results"
echo "publish-results: ${COMMIT} → results, файлов ${NABS}"
