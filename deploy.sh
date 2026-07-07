#!/bin/bash
# МурГав Dashboard — deploy script
# Запускается по cron каждые 2 часа
# Вызывает fetcher.py → data.json → git push → Cloudflare Pages обновится

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/../../../.venv/bin/python3"

# Fallback: system python
if [ ! -f "$VENV" ]; then
  VENV="$(which python3)"
fi

echo "=== МурГав Deploy: $(date '+%Y-%m-%d %H:%M:%S') ==="
echo "Python: $VENV"

# Run fetcher
cd "$SCRIPT_DIR"
"$VENV" fetcher.py

# Git commit & push
if git -C "$SCRIPT_DIR" diff --quiet data.json; then
  echo "data.json не изменился — пропускаем коммит"
else
  git -C "$SCRIPT_DIR" add data.json
  git -C "$SCRIPT_DIR" commit -m "auto: dashboard data $(date '+%Y-%m-%d %H:%M')"
  git -C "$SCRIPT_DIR" push origin main
  echo "✅ Запушено на GitHub"
fi

echo "=== Готово ==="
