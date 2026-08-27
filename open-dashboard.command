#!/bin/zsh
# Double-click to open the GCM dashboard with the freshest data on GitHub.
# Pulls the latest nightly commit, serves the folder locally, opens the browser.
# Nothing here touches Netlify or publishes anything; localhost only.
cd "$(dirname "$0")"

echo "Pulling latest data..."
git pull --ff-only 2>/dev/null || echo "(offline or diverged; showing local copy)"

# Import any affiliate exports sitting in the drop folder. The rollup page reads
# what this writes, and nothing else does, so running it here means a CSV you
# dropped is picked up the next time you open the dashboard rather than needing
# a separate command you have to remember. No-ops silently when the folder is
# empty, which is most of the time.
if [ -n "$(find data/affiliates/drop -maxdepth 1 \( -name '*.csv' -o -name '*.zip' \) -print -quit 2>/dev/null)" ]; then
  echo "Importing affiliate exports..."
  python3 scripts/import_affiliates.py 2>&1 | sed 's/^/  /'
fi

PORT=8994
if ! lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  (python3 -m http.server $PORT --bind 127.0.0.1 >/dev/null 2>&1 &)
  sleep 1
fi

open "http://127.0.0.1:$PORT/"
