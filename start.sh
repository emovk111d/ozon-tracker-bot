#!/usr/bin/env bash
set -e
gunicorn -b 0.0.0.0:$PORT main:app
