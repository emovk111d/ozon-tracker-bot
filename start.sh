#!/usr/bin/env bash
set -e

# запускаем бота в фоне
python bot_runner.py &

# запускаем Flask (порт для Render)
exec gunicorn -b 0.0.0.0:${PORT:-10000} main:app
