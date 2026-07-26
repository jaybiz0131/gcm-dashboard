#!/bin/zsh
# Double-click to open the GCM dashboard with the freshest data on GitHub.
# Pulls the latest nightly commit, serves the folder locally, opens the browser.
# Nothing here touches Netlify or publishes anything; localhost only.
cd "$(dirname "$0")"

echo "Pulling latest data..."
git pull --ff-only 2>/dev/null || echo "(offline or diverged; showing local copy)"

PORT=8994
if ! lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  (python3 -m http.server $PORT --bind 127.0.0.1 >/dev/null 2>&1 &)
  sleep 1
fi

open "http://127.0.0.1:$PORT/"
