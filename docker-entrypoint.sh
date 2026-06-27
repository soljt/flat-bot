#!/bin/sh
set -e

# Start a virtual X display so nodriver can open a real (non-headless) Chrome
# window.  DataDome detects and CAPTCHAs headless browsers, so a virtual display
# is required even in a server/container environment.
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
export DISPLAY=:99

# Brief pause to let Xvfb initialise before Chrome tries to connect
sleep 1

exec uv run python -m flatbot "$@"
