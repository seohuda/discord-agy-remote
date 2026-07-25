#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"

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
if [ -f "$PROFILE_RC" ]; then
    # Portable in-place edit (BSD/macOS sed has no bare -i).
    grep -v "^alias agy=" "$PROFILE_RC" > "$PROFILE_RC.dishell.tmp" || true
    mv "$PROFILE_RC.dishell.tmp" "$PROFILE_RC"
fi
printf '%s\n' "$ALIAS_LINE" >> "$PROFILE_RC"

echo
echo "Done. Restart your terminal or run:"
echo "    source $PROFILE_RC"
