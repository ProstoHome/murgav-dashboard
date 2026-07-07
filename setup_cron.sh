#!/bin/bash
# МурГав Dashboard — регистрация cron-задачи
# Запусти один раз: bash setup_cron.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SH="$SCRIPT_DIR/deploy.sh"
LOG_FILE="$SCRIPT_DIR/deploy.log"

chmod +x "$DEPLOY_SH"

# Добавляем в crontab, если ещё нет
CRON_LINE="0 */2 * * * $DEPLOY_SH >> $LOG_FILE 2>&1"

# Проверяем, не добавлено ли уже
if crontab -l 2>/dev/null | grep -qF "$DEPLOY_SH"; then
  echo "✅ Cron уже настроен"
else
  (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
  echo "✅ Cron добавлен: каждые 2 часа"
  echo "   $CRON_LINE"
fi

echo ""
echo "Текущий crontab:"
crontab -l
