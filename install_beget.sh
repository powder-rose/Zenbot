#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== Zen Bot v34 / Beget Ubuntu =="
echo "Project: $PROJECT_DIR"

if [[ "${EUID}" -ne 0 ]]; then
  SUDO="sudo"
else
  SUDO=""
fi

$SUDO apt-get update
$SUDO apt-get install -y \
  python3 \
  python3-venv \
  python3-pip \
  ca-certificates \
  curl \
  unzip \
  fonts-liberation \
  fonts-dejavu-core

cd "$PROJECT_DIR"

if [[ ! -d venv ]]; then
  python3 -m venv venv
fi

source venv/bin/activate

python -m pip install --upgrade pip wheel setuptools
python -m pip install -r requirements.txt

# Hermetic browser install in the Python environment.
# --with-deps installs Ubuntu dependencies required by Chromium.
PLAYWRIGHT_BROWSERS_PATH=0 \
  python -m playwright install --with-deps chromium

mkdir -p \
  data/images \
  data/telegram_web_profile \
  data/telegram_web_debug

chmod 700 data/telegram_web_profile

echo
echo "Установка завершена."
echo
echo "Дальше:"
echo "1) cp .env.example .env"
echo "2) nano .env"
echo "3) source venv/bin/activate"
echo "4) PLAYWRIGHT_BROWSERS_PATH=0 python setup_telegram_web.py"
echo "5) PLAYWRIGHT_BROWSERS_PATH=0 python check_telegram_web.py"
echo "6) python bot.py"
