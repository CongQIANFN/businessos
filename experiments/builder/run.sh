#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec ../procurement/.venv/bin/python run_builder.py
