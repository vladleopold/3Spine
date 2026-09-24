#!/bin/bash
# Остановить backend и runner.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down