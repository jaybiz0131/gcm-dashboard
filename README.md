# gcm-dashboard

Private traffic and experience command center for the GoCheckMy family (ten properties). Static-baked: a scheduled GitHub Action pulls Cloudflare Web Analytics daily, commits JSON + alerts (and on Sundays a written briefing + a Lighthouse lab pass), and the push redeploys the static page. No live API calls from the browser, no serverless functions, no tokens anywhere near client code.

## What it shows

- **Experience health (top row)**: real-user Core Web Vitals p75 (LCP, INP, CLS) from the Cloudflare beacon over the trailing 28 days, plus the family's worst Lighthouse accessibility score. Traffic-light thresholds (Google's good/poor boundaries), worst property named on every tile.
- **Traffic**: family visits for the last 7 complete days, 28-day daily chart with crosshair tooltip, weekly Haiku summary and movers.
- **Per property**: 7-day visits + WoW, 28-day daily chart, LCP/INP/CLS + accessibility chips, mobile/desktop split, top page.
- **Data table**: every chart's numbers, no hover required.

## Layout

- `index.html` — the dashboard; reads `data/*.json` by relative fetch only
- `data/traffic-history.json` — schema 2: `weeks` (weekly snapshots), `days` (per-day per-site visits/pageviews/device split), `vitals` (daily real-user Core Web Vitals p75 entries), `lab` (weekly Lighthouse performance + accessibility scores)
- `data/alerts-latest.json` — current regression alerts (also rendered as a red strip on the page)
- `reports/traffic-YYYY-MM-DD.md` — weekly written briefing (force-404'd on the web)
- `scripts/pull_traffic.py` — the pull job, Python 3 stdlib only
- `.github/workflows/traffic-pull.yml` — daily ~19:00 ET; Sundays add the weekly report + Lighthouse pass

## Setup (one time)

1. Repo secrets (Settings > Secrets and variables > Actions):
   - `CLOUDFLARE_ANALYTICS_TOKEN` — read-only token with Account Analytics: Read
   - `ANTHROPIC_API_KEY` — for the 2-3 sentence weekly summary (optional; a deterministic fallback is used without it)
   - (no key needed for Lighthouse: the Sunday pass runs the lighthouse CLI on the GitHub runner's preinstalled Chrome)
2. Run the "Traffic pull" workflow once from the Actions tab (check "force weekly" to also get the report + Lighthouse on a non-Sunday).
3. To view: double-click `open-dashboard.command` (pulls the latest nightly commit, serves the folder on localhost, opens the browser). Netlify is optional and unlinked; link it only if phone/anywhere access is ever wanted (build command blank, publish directory `.`, netlify.toml already says so).

## Behavior notes

- Daily series and vitals cover complete UTC days only, so re-runs of the same day write identical values (idempotent). Same-week weekly re-runs replace that week's entry, never duplicate it.
- A property returning nothing is recorded honestly (no beacon events = awaiting signal); one dark site never fails the run.
- A failed run opens a GitHub Issue and commits nothing; the published data is never left partial or corrupt.
- **Regression alerts are transition-based**: a metric opens an issue the day it turns poor (LCP > 4s, INP > 500ms, CLS > 0.25, accessibility < 90), then stays quiet while it remains poor. Vitals with under 10 samples never alert.
- Local dry run without touching the network: `GCM_MOCK=1 GCM_ROOT=/tmp/somewhere python3 scripts/pull_traffic.py`
- Not measurable from here (needs per-site instrumentation, deliberately out of scope): session replay, rage clicks, funnels, task success, scroll depth, SUS surveys.
