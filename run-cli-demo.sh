#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$ROOT/experiments/procurement/.venv/bin/python" "$ROOT/examples/cli_demo.py"
