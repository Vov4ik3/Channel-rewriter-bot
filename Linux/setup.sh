#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
echo "=== Channel Rewriter setup ==="

if [ ! -d venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi
echo "Installing packages..."
venv/bin/python -m pip install --upgrade pip >/dev/null
# pystray/pillow are only for Windows tray mode; skip failures on headless Linux
venv/bin/python -m pip install telethon google-genai python-dotenv
venv/bin/python -m pip install pystray pillow || echo "(tray packages skipped - not needed on Linux)"

if [ ! -f .env ]; then
    cp .env.example .env
    echo
    echo "Created .env - fill it in, then re-run ./setup.sh"
    echo "  nano .env"
    exit 0
fi

if [ ! -f reader.session ]; then
    echo
    echo "=== Logging in the reader account ==="
    venv/bin/python login.py
fi
echo
echo "Done. Start the bot with ./run.sh (or install the systemd service, see README)."
