#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"

if ! command -v python3 >/dev/null; then
    echo "python3 not found. Install Python 3.10 or newer first." >&2
    exit 1
fi

# The code uses 3.10 syntax, which older interpreters reject at import time
# with an error that says nothing about the version.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python 3.10 or newer is required (found $(python3 -V))." >&2
    exit 1
fi

if ! command -v agy >/dev/null; then
    echo "Warning: 'agy' is not on your PATH. Install the Antigravity CLI before using this." >&2
fi

echo "==> Creating virtualenv at $VENV"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$ROOT/requirements.txt"

if [ ! -f "$ROOT/.env" ]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    echo "==> Created .env from .env.example — edit it and fill in your token/user id."
fi

case "${SHELL##*/}" in
    zsh)  PROFILE_RC="$HOME/.zshrc" ;;
    bash) PROFILE_RC="$HOME/.bashrc" ;;
    *)    PROFILE_RC="$HOME/.profile" ;;
esac

ALIAS_LINE="alias agy='\"$VENV/bin/python\" \"$ROOT/main.py\"'"

echo "==> Registering 'agy' alias in $PROFILE_RC"
if [ -f "$PROFILE_RC" ] && grep -q "^alias agy=" "$PROFILE_RC"; then
    # Portable in-place edit (BSD/macOS sed has no bare -i). The rewrite is
    # only swapped in once it is written, so a failure cannot truncate the rc.
    TMP="$PROFILE_RC.dishell.tmp"
    cp "$PROFILE_RC" "$PROFILE_RC.dishell.bak"
    grep -v "^alias agy=" "$PROFILE_RC" > "$TMP" || true
    mv "$TMP" "$PROFILE_RC"
    echo "==> Replaced an existing 'agy' alias (backup: $PROFILE_RC.dishell.bak)"
fi
printf '%s\n' "$ALIAS_LINE" >> "$PROFILE_RC"

echo
echo "Done. Restart your terminal or run:"
echo "    source $PROFILE_RC"
