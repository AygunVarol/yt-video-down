#!/usr/bin/env sh
# Start the video downloader on macOS / Linux:  ./start.sh
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "First run: setting up... this takes a minute."
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip >/dev/null
    .venv/bin/python -m pip install -r requirements.txt
fi

# Keep yt-dlp current - YouTube changes often and old versions stop working.
.venv/bin/python -m pip install -q --upgrade "yt-dlp[default]" >/dev/null 2>&1 || true

exec .venv/bin/python app.py
