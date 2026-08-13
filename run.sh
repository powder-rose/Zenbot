#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

source venv/bin/activate

export PYTHONUNBUFFERED=1
export PLAYWRIGHT_BROWSERS_PATH=0

exec python bot.py
