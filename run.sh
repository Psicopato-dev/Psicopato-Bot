#!/data/data/com.termux/files/usr/bin/bash
# Запуск бота в Termux с авто-перезапуском при падении.
cd "$(dirname "$0")"
termux-wake-lock 2>/dev/null
while true; do
  python bot.py
  echo "Бот остановился, перезапуск через 5 с (Ctrl+C — выход)..." >&2
  sleep 5
done
