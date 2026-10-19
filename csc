#!/bin/sh
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
exec "$here/.venv/bin/python" "$here/samregionhop.py" "$@"
