#!/bin/sh
# Run from any working directory; keep the key and Python environment here.
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
if [ ! -f .env ]; then
  cp .env.example .env
  printf '%s\n' 'master/.env에 GEMINI_API_KEY를 입력하고 다시 실행하세요.'
  exit 0
fi
printf '%s\n' 'Chrome에서 http://localhost:8000/ 을 여세요. 종료: Ctrl+C'
exec .venv/bin/python server.py "$@"
