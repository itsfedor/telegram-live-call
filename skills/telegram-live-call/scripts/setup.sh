#!/usr/bin/env bash
set -euo pipefail
PACKAGE='git+https://github.com/itsfedor/telegram-live-call.git@v0.1.0'
if command -v uv >/dev/null 2>&1; then
  uv tool install --python 3.13 "$PACKAGE"
else
  command -v python3.13 >/dev/null 2>&1 || { printf '%s\n' 'Install Python 3.13+ or uv first.' >&2; exit 1; }
  VENV="${XDG_DATA_HOME:-$HOME/.local/share}/telegram-live-call/venv"
  python3.13 -m venv "$VENV"
  "$VENV/bin/python" -m pip install "$PACKAGE"
  printf 'Installed. Add this directory to PATH: %s\n' "$VENV/bin"
fi
printf '%s\n' 'Next: configure a private .env and run telegram-live-call --env-file .env login yourself.'
