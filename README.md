# gcm-dashboard

Private traffic command center for the GoCheckMy family (eight properties). Static-baked: a scheduled GitHub Action pulls Cloudflare Web Analytics weekly, commits JSON + a written briefing, and the push redeploys the static page. No live API calls from the browser, no serverless functions, no tokens anywhere near client code.

## Layout

- `index.html` — the dashboard; reads `data/traffic-history.json` by relative fetch only
- `data/traffic-history.json` — accumulating weekly snapshots (append-only history, one entry per ISO week)
- `reports/traffic-YYYY-MM-DD.md` — weekly written briefing (force-404'd on the web)
- `scripts/pull_traffic.py` — the pull job, Python 3 stdlib only
- `.github/workflows/traffic-pull.yml` — Sundays ~19:00 ET, plus manual runs via workflow_dispatch

## Setup (one time)

1. Repo secrets (Settings > Secrets and variables > Actions):
   - `CLOUDFLARE_ANALYTICS_TOKEN` — read-only token with Account Analytics: Read
   - `ANTHROPIC_API_KEY` — for the 2-3 sentence weekly summary (optional; a deterministic fallback is used without it)
2. Run the "Traffic pull" workflow once from the Actions tab so the first snapshot exists.
3. Link to Netlify: build command blank, publish directory `.` (netlify.toml already says so).

## Behavior notes

- Same-week re-runs replace that week's entry, never duplicate it.
- A property returning nothing is recorded as `no_data: true` with a note; one dark site never fails the run.
- A failed run opens a GitHub Issue and commits nothing; the published data is never left partial or corrupt.
- Flip to daily: change the cron line in the workflow (comment marks it).
- Local dry run without touching the network: `GCM_MOCK=1 GCM_ROOT=/tmp/somewhere python3 scripts/pull_traffic.py`
