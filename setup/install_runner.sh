#!/bin/bash
#
# Install GitHub Actions self-hosted runner on Linux (x64).
#
# Usage:
#   ./install_runner.sh OWNER/REPO REGISTRATION_TOKEN
#
# Token: GitHub repo -> Settings -> Actions -> Runners -> New self-hosted runner.
#
set -euo pipefail

REPO="${1:?usage: install_runner.sh OWNER/REPO TOKEN}"
TOKEN="${2:?usage: install_runner.sh OWNER/REPO TOKEN}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This script is for Linux only (got $(uname -s))." >&2
  exit 1
fi

# --- resolve latest runner version via GitHub release redirect ---
LOC=$(curl -sI https://github.com/actions/runner/releases/latest | tr -d '\r' | grep -i '^location:' | awk '{print $2}')
VER=$(echo "$LOC" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
if [[ -z "$VER" ]]; then
  echo "Could not resolve latest runner version." >&2
  exit 1
fi
echo "Latest runner version: $VER"

mkdir -p "$HOME/actions-runner"
curl -sL -o "$HOME/actions-runner/actions-runner.tar.gz" \
  "https://github.com/actions/runner/releases/download/v${VER}/actions-runner-linux-x64-${VER}.tar.gz"
tar xzf "$HOME/actions-runner/actions-runner.tar.gz" -C "$HOME/actions-runner"

cd "$HOME/actions-runner"
./configure.sh --url "https://github.com/$REPO" --token "$TOKEN" --unattended --replace
sudo ./svc.sh install
sudo ./svc.sh start

echo
echo "Self-hosted runner installed and started for $REPO."
echo "Runner label: 'self-hosted, linux, X64'"