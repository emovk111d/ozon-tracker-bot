#!/usr/bin/env bash
set -e

# Flask для Render (порт)
gunicorn -b 0.0.0.0:$PORT main:app &

# Телеграм бот как главный процесс
python main.py
