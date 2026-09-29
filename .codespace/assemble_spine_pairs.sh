#!/usr/bin/env bash
# Сборка input.zip из каталога с Spine-парами. Общий шаг для всех
# экстракторов: подрезка под лимит GitHub, индекс превью, архив.
#
#   assemble_spine_pairs.sh <каталог с spine/> <рабочий корень> [cap_mb]
#
# Возвращает 0 и печатает число проектов, если собрать удалось.
set -uo pipefail

SRC="${1:?нужен каталог с spine/}"
WS="${2:-$PWD}"
CAP_MB="${3:-85}"
WORK="$WS/pg_out"

# Скрипты берём от себя, а не от $WS: рабочий корень может быть отдельным
# каталогом (в CI он совпадает с репозиторием, но полагаться на это нельзя).
SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

N=$(find "$SRC" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
if [ "$N" -eq 0 ]; then
  echo "::warning::в $SRC нет ни одного проекта"
  exit 1
fi
echo "проектов на входе: $N"

rm -f "$WS/input.zip"
rm -rf "$WORK"
mkdir -p "$WORK"
cp -R "$SRC" "$WORK/spine"

# Отдельные textures/ не копируем: страницы атласов уже лежат внутри каждого
# проекта, а сами спрайты вдвое раздувают архив и упираются в лимит GitHub.
# Если всё равно не влезаем — trim_spine.py сначала снимает добавленные из
# общего пула текстур страницы у крупных проектов, и только потом удаляет
# проект целиком.
python3 "$SELF_DIR/pg-extractor/trim_spine.py" "$WORK/spine" "$CAP_MB"

N=$(find "$WORK/spine" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
if [ "$N" -eq 0 ]; then
  echo "::warning::после подрезки не осталось ни одного проекта"
  exit 1
fi

python3 "$SELF_DIR/pg-extractor/make_previews_index.py" "$WORK" || true
( cd "$WORK" && zip -qr "$WS/input.zip" . )
echo "input.zip собран: $(unzip -l "$WS/input.zip" | tail -1)"
echo "$N"
