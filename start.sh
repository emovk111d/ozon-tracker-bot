#!/usr/bin/env bash
set -e

# веб для Render (порт)
gunicorn -b 0.0.0.0:$PORT main:app &

# телеграм polling как главный процесс
python main.py
