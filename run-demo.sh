#!/bin/sh
set -eu
cd "$(dirname "$0")"

for command in python3 initdb pg_ctl; do
    if ! command -v "$command" >/dev/null 2>&1; then
        printf 'Missing %s. See README.md for local prerequisites.\n' "$command" >&2
        exit 1
    fi
done

python=experiments/procurement/.venv/bin/python
if [ ! -x "$python" ]; then
    python3 -m venv experiments/procurement/.venv
fi
if ! "$python" -c 'import psycopg' 2>/dev/null; then
    "$python" -m pip install -r experiments/procurement/requirements.txt
fi

exec "$python" demo.py
