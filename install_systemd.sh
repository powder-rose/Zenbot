#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="${1:-zenbot}"
RUN_USER="${SUDO_USER:-$USER}"

if [[ "${EUID}" -ne 0 ]]; then
  SUDO="sudo"
else
  SUDO=""
fi

SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

$SUDO tee "$SERVICE_PATH" >/dev/null <<EOF
[Unit]
Description=Zen Telegram Content Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${PROJECT_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=PLAYWRIGHT_BROWSERS_PATH=0
ExecStart=${PROJECT_DIR}/venv/bin/python ${PROJECT_DIR}/bot.py
Restart=always
RestartSec=10
TimeoutStopSec=30

# Telegram Web profile contains an authenticated user session.
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_NAME"

echo "Systemd service создан: $SERVICE_NAME"
echo "Запуск: sudo systemctl start $SERVICE_NAME"
echo "Статус: sudo systemctl status $SERVICE_NAME"
echo "Логи: sudo journalctl -u $SERVICE_NAME -f"
