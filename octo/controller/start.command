#!/bin/sh
# 중앙 컴퓨터(맥)에서 더블클릭 또는 `sh octo/controller/start.command`
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python server.py "$@"
