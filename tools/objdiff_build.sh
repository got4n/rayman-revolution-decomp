#!/bin/sh
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
export PATH
cd "$(dirname "$0")/.." || exit 1
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python tools/objdiff_build.py "$@"
fi
exec python3 tools/objdiff_build.py "$@"
