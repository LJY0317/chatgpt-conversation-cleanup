#!/bin/zsh
set -eu

cd -- "${0:A:h}"
export PYTHONDONTWRITEBYTECODE=1
exec python3 run.py "$@"
