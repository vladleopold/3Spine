#!/bin/bash
#
# Поднять всю инфраструктуру (Docker, Linux):
#   backend — HTTP-сервис конвертации :8080
#   runner  — GitHub Actions self-hosted runner (Linux) для vladleopold/3Spine
#
# Перед запуском: cp .env.example .env  и впишите ACCESS_TOKEN.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

if [ ! -f .env ]; then
  echo "Скопируйте .env.example в .env и впишите ACCESS_TOKEN (Personal Access Token, repo)." >&2
  exit 1
fi

echo "==> backend (Linux container) ..."
docker compose up -d --build backend
sleep 3

echo "==> self-hosted runner (Linux container) ..."
docker compose up -d runner

echo
echo "Проверка backend:"
curl -s http://127.0.0.1:8080/health || echo "(сервер ещё поднимается — повторите)"
echo
echo "Состояние: docker compose ps"