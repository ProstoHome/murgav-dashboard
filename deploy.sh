#!/bin/bash
# МурГав Dashboard — deploy script
# Запускается launchd каждые 2 часа (com.aiagent.murgav-dashboard)
# fetcher.py → data.json → git push → GitHub Pages обновится

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
  # push с жёстким таймаутом — git push однажды завис >3 мин и завалил задачу
  git -C "$SCRIPT_DIR" push origin main &
  PUSH_PID=$!
  WAITED=0
  while kill -0 "$PUSH_PID" 2>/dev/null && [ "$WAITED" -lt 60 ]; do
    sleep 2; WAITED=$((WAITED+2))
  done
  if kill -0 "$PUSH_PID" 2>/dev/null; then
    kill -9 "$PUSH_PID" 2>/dev/null || true
    echo "⚠️ push завис >60с — прерван, повторится в следующем цикле"
  else
    wait "$PUSH_PID" && echo "✅ Запушено на GitHub" \
      || echo "⚠️ push не прошёл (повторится в следующем цикле)"
  fi
fi

echo "=== Готово ==="
