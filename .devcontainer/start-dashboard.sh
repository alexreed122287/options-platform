#!/usr/bin/env bash
# Auto-start the dashboard server inside a Codespace.
#
# Idempotent: if something is already listening on the dashboard port, do
# nothing. Otherwise launch the server fully detached with setsid + nohup so
# it survives the postStart lifecycle hook's shell exiting (the reason a plain
# backgrounded process could die). Output goes to /tmp/options-platform.log.
set -uo pipefail

cd /workspaces/options-platform 2>/dev/null || cd "$(dirname "$0")/.." || exit 0

PORT="${PORT:-8787}"
LOG=/tmp/options-platform.log

# curl returns 0 on ANY HTTP response (including 401 from the token gate) and
# non-zero only when nothing is listening -> use it as a "already up?" probe.
if curl -s -o /dev/null "http://localhost:${PORT}/"; then
  echo "[start-dashboard] already running on :${PORT}, skipping" >> "$LOG"
  exit 0
fi

echo "[start-dashboard] launching server on :${PORT}" > "$LOG"
setsid nohup python run.py >> "$LOG" 2>&1 < /dev/null &
echo "[start-dashboard] launched pid $!" >> "$LOG"
