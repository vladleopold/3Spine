#!/usr/bin/env bash
# Одноразовая установка self-hosted GitHub Actions runner на macOS (Intel x64).
# Запуск: bash .codespace/setup-selfhosted-runner.sh
# После установки зарегистрируй runner командой, которую GitHub даёт на странице:
#   Settings → Actions → Runners → New self-hosted runner  (токен короткоживущий)
# Затем запусти: ./run.sh   (или запусти как service, см. ./svc.sh install)
set -euo pipefail

RUNNER_VERSION="2.337.0"
RUNNER_DIR="${HOME}/actions-runner"

if [ -d "${RUNNER_DIR}" ]; then
  echo "Скрипт-раннер уже есть в ${RUNNER_DIR}"
else
  echo "→ Скачиваю actions-runner-osx-x64-${RUNNER_VERSION}"
  mkdir -p "${RUNNER_DIR}"
  curl -o "${RUNNER_DIR}/runner.tar.gz" -L \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-osx-x64-${RUNNER_VERSION}.tar.gz"
  tar xzf "${RUNNER_DIR}/runner.tar.gz" -C "${RUNNER_DIR}"
  rm -f "${RUNNER_DIR}/runner.tar.gz"
  echo "→ Доп. зависимости для macOS не требуются."
fi

echo ""
echo "Дальше на https://github.com/vladleopold/3Spine/settings/actions/runners"
echo "нажми «New self-hosted runner» → macOS → x64 — и выполни там:"
echo ""
echo "  cd ${RUNNER_DIR}"
echo "  ./config.sh --url https://github.com/vladleopold/3Spine --token <ТОКЕН>"
echo "  ./run.sh"
echo ""
echo "Лейбл по умолчанию содержит self-hosted — воркфлоу web-convert.yml ждёт именно его."