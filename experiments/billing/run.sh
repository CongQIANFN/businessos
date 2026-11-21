#!/bin/sh
set -eu
cd "$(dirname "$0")"
shared_python="../procurement/.venv/bin/python"
if [ ! -x "$shared_python" ]; then
  echo 'Missing shared procurement Python environment. Prepare experiments/procurement/.venv first.' >&2
  exit 2
fi
exec "$shared_python" run_experiment.py "$@"
