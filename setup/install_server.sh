#!/bin/bash
#
# Install the Spine Converter HTTP service on Linux.
# Copies the backend + native converter, registers a systemd service on :8080.
#
set -euo pipefail

APP_DIR="/opt/spine-converter"
PORT="${SPINE_PORT:-8080}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $(id -u) -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

echo "==> Preparing $APP_DIR ..."
mkdir -p "$APP_DIR/converter"
cp -R "$SRC/backend"/* "$APP_DIR/"
chmod +x "$APP_DIR/converter/SpineSkeletonDataConverter"
chmod +x "$APP_DIR/converter.py" "$APP_DIR/server.py"

echo "==> Verifying converter binary ..."
"$APP_DIR/converter/SpineSkeletonDataConverter" --help >/dev/null 2>&1 \
  && echo "    converter binary runs OK" \
  || echo "    (warning) converter did not print help — will still be checked at runtime"

echo "==> Creating systemd unit ..."
cat > /etc/systemd/system/spine-converter.service <<EOF
[Unit]
Description=Spine Converter HTTP service v1.0
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/server.py
Environment=SPINE_PORT=$PORT
Environment=SPINE_RESULT_DIR=$APP_DIR/results
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now spine-converter
sleep 1
systemctl --no-pager status spine-converter | head -8

echo
echo "==> Firewall (run if needed):"
echo "    sudo ufw allow $PORT/tcp"
echo
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "Backend is now serving: http://${IP:-<SERVER_IP>}:$PORT/health"
echo "Put this address into the web GUI (frontend) and press Save."