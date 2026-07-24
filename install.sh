#!/bin/bash
set -e

# Get absolute path to main.py
MAIN_PY_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/main.py"

# Add alias to .bashrc or .zshrc
PROFILE_RC="$HOME/.bashrc"
if [ -f "$HOME/.zshrc" ]; then
    PROFILE_RC="$HOME/.zshrc"
fi

echo "Adding 'agy' alias to $PROFILE_RC to route through dishell..."

# Remove old alias if exists
sed -i '/alias agy=/d' "$PROFILE_RC"

# Add new alias
echo "alias agy='python3 \"$MAIN_PY_PATH\"'" >> "$PROFILE_RC"

echo "Installation complete! Please restart your terminal or run:"
echo "source $PROFILE_RC"
