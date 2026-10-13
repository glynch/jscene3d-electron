#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# This interpreter runs only the guard program. Build actions receive the
# checkout-managed Python selected and validated by build_electron.py.
PYTHON=/usr/bin/python3

if [ ! -x "$PYTHON" ]; then
	printf '%s\n' "JScene3D Electron build error: required Python executable not found: $PYTHON" >&2
	exit 2
fi

exec "$PYTHON" "$SCRIPT_DIR/build_electron.py" "$@"
