#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -c 'import psycopg' 2>/dev/null || .venv/bin/pip install -r requirements.txt
exec .venv/bin/python run_experiment.py "$@"
