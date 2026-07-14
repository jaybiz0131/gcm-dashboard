#!/usr/bin/env python3
"""GCM traffic pull.

Pulls trailing-7-day Cloudflare Web Analytics for all eight properties,
appends an idempotent weekly snapshot to data/traffic-history.json, and
writes reports/traffic-YYYY-MM-DD.md.

Stdlib only. Secrets come from the environment:
  CLOUDFLARE_ANALYTICS_TOKEN  read-only Analytics token (required)
  ANTHROPIC_API_KEY           for the weekly Haiku summary (optional;
                              a deterministic fallback summary is used
                              if absent or the call fails)

Local testing:
  GCM_MOCK=1     deterministic fake Cloudflare data, no network at all
  GCM_ROOT=dir   write data/ and reports/ under dir instead of the repo
  GCM_NOW=date   pretend "now" is this ISO date (mock/testing only)
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SITES = [
    "gocheckmy.com",
    "gocheckmyhome.com",
    "gocheckmypet.com",
    "gocheckmymortgage.com",
    "gocheckmyparents.com",
    "gocheckmyestate.com",
    "gocheckmystorm.com",
    "gocheckmycrypto.com",
]

CF_API = "https://api.cloudflare.com/client/v4"
CF_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

ET = ZoneInfo("America/New_York")

ROOT = os.environ.get("GCM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_PATH = os.path.join(ROOT, "data", "traffic-history.json")
REPORTS_DIR = os.path.join(ROOT, "reports")

MOCK = os.environ.get("GCM_MOCK") == "1"


def log(msg):
    print(msg, flush=True)


def now_et():
    override = os.environ.get("GCM_NOW")
    if override:
        return datetime.fromisoformat(override).replace(tzinfo=ET)
    return datetime.now(tz=ET)


def http_json(url, method="GET", headers=None, body=None, timeout=30, retries=2):
    """JSON request with small retry for transient failures."""
    data = json.dumps(body).encode() if body is not None else None
    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:500]
            except Exception:
                pass
            last_err = RuntimeError(f"HTTP {e.code} from {url}: {detail}")
            if e.code < 500 and e.code != 429:
                break  # client error, retrying will not help
        except Exception as e:
            last_err = RuntimeError(f"{type(e).__name__} calling {url}: {e}")
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise last_err


# ---------------------------------------------------------------- Cloudflare

def cf_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def normalize_host(host):
    host = (host or "").strip().lower()
    return host[4:] if host.startswith("www.") else host


def discover_sites(token, since, until):
    """Map hostname -> (account_tag, site_tag) across every account the token can see.

    Discovery goes through GraphQL (grouped by siteTag and requestHost over the pull
    window) because an Analytics-Read token is not allowed to call the REST
    rum/site_info endpoint. Netlify deploy-preview hosts share the production
    siteTag, so only hostnames on the SITES roster make it into the map. A site
    with zero pageloads in the window simply does not appear, which downstream
    records as no_data. That is honest: no beacon events means nothing to report.
    """
    accounts = http_json(f"{CF_API}/accounts?per_page=50", headers=cf_headers(token))
    if not accounts.get("success"):
        raise RuntimeError(f"Cloudflare account listing failed: {accounts.get('errors')}")
    wanted = set(SITES)
    mapping = {}
    for acct in accounts.get("result") or []:
        query = f"""
        {{
          viewer {{
            accounts(filter: {{accountTag: "{acct['id']}"}}) {{
              rumPageloadEventsAdaptiveGroups(
                filter: {{datetime_geq: "{since}", datetime_lt: "{until}"}},
                orderBy: [sum_visits_DESC], limit: 500
              ) {{
                sum {{ visits }}
                dimensions {{ siteTag requestHost }}
              }}
            }}
          }}
        }}
        """
        resp = http_json(CF_GRAPHQL, method="POST", headers=cf_headers(token), body={"query": query})
        if resp.get("errors"):
            raise RuntimeError(f"GraphQL discovery error: {resp['errors']}")
        for a in (resp.get("data") or {}).get("viewer", {}).get("accounts") or []:
            for g in a.get("rumPageloadEventsAdaptiveGroups") or []:
                host = normalize_host(g["dimensions"].get("requestHost"))
                if host in wanted:
                    mapping.setdefault(host, (acct["id"], g["dimensions"]["siteTag"]))
    return mapping


def pull_site(token, account_tag, site_tag, host, since, until):
    """Trailing-window visits, pageviews, and top paths for one property.

    Pinned to the production hostnames: the siteTag also collects hits on
    Netlify deploy-preview subdomains, which are not real traffic.
    """
    flt = (
        f'{{siteTag: "{site_tag}", requestHost_in: ["{host}", "www.{host}"], '
        f'datetime_geq: "{since}", datetime_lt: "{until}"}}'
    )
    query = f"""
    {{
      viewer {{
        accounts(filter: {{accountTag: "{account_tag}"}}) {{
          totals: rumPageloadEventsAdaptiveGroups(filter: {flt}, limit: 1) {{
            count
            sum {{ visits }}
          }}
          topPaths: rumPageloadEventsAdaptiveGroups(
            filter: {flt}, orderBy: [count_DESC], limit: 6
          ) {{
            count
            sum {{ visits }}
            dimensions {{ requestPath }}
          }}
        }}
      }}
    }}
    """
    resp = http_json(CF_GRAPHQL, method="POST", headers=cf_headers(token), body={"query": query})
    if resp.get("errors"):
        raise RuntimeError(f"GraphQL error: {resp['errors']}")
    accounts = (resp.get("data") or {}).get("viewer", {}).get("accounts") or []
    if not accounts:
        raise RuntimeError("GraphQL returned no account data")
    acct = accounts[0]
    totals = acct.get("totals") or []
    visits = totals[0]["sum"]["visits"] if totals else 0
    pageviews = totals[0]["count"] if totals else 0
    top_paths = [
        {"path": g["dimensions"]["requestPath"] or "/", "pageviews": g["count"]}
        for g in (acct.get("topPaths") or [])
        if g.get("dimensions")
    ][:3]
    return {"visits": visits, "pageviews": pageviews, "top_paths": top_paths, "no_data": False}


# ---------------------------------------------------------------- mock mode

def mock_pull(site, week_key):
    """Deterministic fake numbers so idempotency tests are meaningful."""
    seed = int(hashlib.sha256(f"{site}|{week_key}".encode()).hexdigest()[:8], 16)
    if site == "gocheckmystorm.com":  # exercise the no_data path
        return {"visits": 0, "pageviews": 0, "top_paths": [], "no_data": True,
                "note": "mock: no Web Analytics property found for this hostname"}
    visits = 20 + seed % 400
    return {
        "visits": visits,
        "pageviews": int(visits * (1.4 + (seed % 7) / 10)),
        "top_paths": [
            {"path": "/", "pageviews": int(visits * 0.6)},
            {"path": "/guides/", "pageviews": int(visits * 0.3)},
            {"path": "/about/", "pageviews": int(visits * 0.1)},
        ],
        "no_data": False,
    }


# ---------------------------------------------------------------- summary

def fmt_pct(cur, prev):
    if prev in (None, 0):
        return None
    return round((cur - prev) / prev * 100)


def fallback_summary(week, prev_week):
    total = sum(s["visits"] for s in week["sites"].values())
    live = sum(1 for s in week["sites"].values() if not s.get("no_data"))
    parts = [f"The family logged {total} visits across {live} reporting sites this week."]
    if prev_week:
        prev_total = sum(s["visits"] for s in prev_week["sites"].values())
        pct = fmt_pct(total, prev_total)
        if pct is not None:
            direction = "up" if pct >= 0 else "down"
            parts.append(f"That is {direction} {abs(pct)}% from last week's {prev_total}.")
    dark = [h for h, s in week["sites"].items() if s.get("no_data")]
    if dark:
        parts.append(f"Still awaiting signal from {', '.join(dark)}.")
    return " ".join(parts)


def haiku_summary(api_key, week, prev_week):
    payload = {
        "this_week": {h: {"visits": s["visits"], "pageviews": s["pageviews"],
                          "top_path": (s["top_paths"][0]["path"] if s["top_paths"] else None),
                          "no_data": s.get("no_data", False)}
                      for h, s in week["sites"].items()},
        "last_week": {h: s["visits"] for h, s in (prev_week or {}).get("sites", {}).items()} or None,
        "week_ending": week["week_ending"],
    }
    prompt = (
        "You write a one-breath weekly traffic note for the owner of a small family of websites. "
        "Here is this week's data (visits are the trailing 7 days):\n\n"
        + json.dumps(payload, indent=1)
        + "\n\nWrite 2-3 plain-English sentences: what moved and by roughly how much, any site "
        "that got its first traffic, anything notable. Total and biggest changes first. "
        "No hype, no lists, no markdown, and never use an em dash."
    )
    resp = http_json(
        ANTHROPIC_API,
        method="POST",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
        body={"model": HAIKU_MODEL, "max_tokens": 300,
              "messages": [{"role": "user", "content": prompt}]},
    )
    text = " ".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
    if not text:
        raise RuntimeError("empty summary from model")
    return text.replace("—", ",").replace(" ,", ",")


# ---------------------------------------------------------------- notables

def site_series(weeks, host):
    return [(w["week_ending"], (w["sites"].get(host) or {}).get("visits", 0),
             (w["sites"].get(host) or {}).get("no_data", True)) for w in weeks]


def compute_notables(weeks):
    """Streaks, movers, firsts, records, from accumulated history. No model call."""
    notes = []
    if len(weeks) < 2:
        notes.append("First snapshot on record. Deltas, streaks, and movers start next week.")
        return notes
    cur, prev = weeks[-1], weeks[-2]

    total_cur = sum(s["visits"] for s in cur["sites"].values())
    total_prev = sum(s["visits"] for s in prev["sites"].values())
    pct = fmt_pct(total_cur, total_prev)
    if pct is not None:
        notes.append(f"Family total {'up' if pct >= 0 else 'down'} {abs(pct)}% week over week "
                     f"({total_prev} to {total_cur} visits).")

    best_host, best_pct = None, None
    for host in SITES:
        c = (cur["sites"].get(host) or {}).get("visits", 0)
        p = (prev["sites"].get(host) or {}).get("visits", 0)
        if p >= 5:
            change = fmt_pct(c, p)
            if change is not None and (best_pct is None or abs(change) > abs(best_pct)):
                best_host, best_pct = host, change
    if best_host is not None and best_pct != 0:
        notes.append(f"Biggest mover: {best_host}, {'+' if best_pct >= 0 else ''}{best_pct}% "
                     f"({(prev['sites'].get(best_host) or {}).get('visits', 0)} to "
                     f"{(cur['sites'].get(best_host) or {}).get('visits', 0)} visits).")

    for host in SITES:
        series = site_series(weeks, host)
        cur_v = series[-1][1]
        if cur_v > 0 and all(v == 0 for _, v, _ in series[:-1]):
            notes.append(f"First traffic on record for {host}: {cur_v} visits.")
        elif len(series) >= 4 and cur_v > 0 and cur_v > max(v for _, v, _ in series[:-1]):
            notes.append(f"Best week on record for {host}: {cur_v} visits.")

    for host in SITES:
        series = [v for _, v, _ in site_series(weeks, host)]
        streak = 0
        for i in range(len(series) - 1, 0, -1):
            if series[i] > series[i - 1] > 0 or (series[i] > series[i - 1] and series[i - 1] > 0):
                streak += 1
            else:
                break
        if streak >= 3:
            notes.append(f"{host} has grown {streak} weeks in a row.")

    return notes


# ---------------------------------------------------------------- report

def build_report(weeks):
    cur = weeks[-1]
    prev = weeks[-2] if len(weeks) >= 2 else None
    lines = [
        f"# GCM traffic briefing, week ending {cur['week_ending']}",
        "",
        cur["summary"],
        "",
        "| Site | Visits | WoW | Top page |",
        "|---|---:|---:|---|",
    ]
    for host in SITES:
        s = cur["sites"].get(host) or {}
        if s.get("no_data"):
            lines.append(f"| {host} | no data | n/a | n/a |")
            continue
        p = (prev["sites"].get(host) or {}).get("visits") if prev else None
        pct = fmt_pct(s.get("visits", 0), p)
        wow = f"{'+' if pct >= 0 else ''}{pct}%" if pct is not None else "n/a"
        top = s["top_paths"][0] if s.get("top_paths") else None
        top_txt = f"`{top['path']}` ({top['pageviews']})" if top else "n/a"
        lines.append(f"| {host} | {s.get('visits', 0)} | {wow} | {top_txt} |")
    lines += ["", "## Notable", ""]
    lines += [f"- {n}" for n in compute_notables(weeks)]
    lines += ["", f"_Pulled {cur['pulled_at']} from Cloudflare Web Analytics. "
              f"Summary source: {cur.get('summary_source', 'unknown')}._", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    if path.endswith(".json"):
        json.loads(text)  # never move a corrupt file into place
    os.replace(tmp, path)


def main():
    now = now_et()
    until = now.astimezone(timezone.utc)
    since = until - timedelta(days=7)
    iso = now.isocalendar()
    week_key = f"{iso.year}-W{iso.week:02d}"
    week_ending = now.date().isoformat()
    since_s = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_s = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"Pull window {since_s} to {until_s} (week {week_key}, ending {week_ending})")

    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            history = json.load(f)
    else:
        history = {"schema": 1, "weeks": []}

    mapping = {}
    if MOCK:
        log("MOCK mode: no network calls will be made.")
    else:
        token = os.environ.get("CLOUDFLARE_ANALYTICS_TOKEN")
        if not token:
            raise RuntimeError("CLOUDFLARE_ANALYTICS_TOKEN is not set")
        mapping = discover_sites(token, since_s, until_s)
        log(f"Discovered {len(mapping)} Web Analytics properties: {sorted(mapping)}")

    sites_out = {}
    for host in SITES:
        try:
            if MOCK:
                sites_out[host] = mock_pull(host, week_key)
            elif host not in mapping:
                sites_out[host] = {"visits": 0, "pageviews": 0, "top_paths": [], "no_data": True,
                                   "note": "no pageload events for this hostname in the window"}
            else:
                acct, tag = mapping[host]
                sites_out[host] = pull_site(token, acct, tag, host, since_s, until_s)
        except Exception as e:
            log(f"WARN {host}: {e}")
            sites_out[host] = {"visits": 0, "pageviews": 0, "top_paths": [], "no_data": True,
                               "note": f"pull failed: {e}"[:300]}
        s = sites_out[host]
        log(f"  {host}: " + ("no data" if s["no_data"] else f"{s['visits']} visits, {s['pageviews']} pageviews"))

    # A quiet site is fine; every site failing means the API or token is broken.
    # Bail before writing anything so a systemic failure never publishes a dark week.
    if all(s.get("no_data") for s in sites_out.values()) and \
       any("pull failed" in s.get("note", "") for s in sites_out.values()):
        raise RuntimeError("every property failed to pull; refusing to commit an all-dark week")

    snapshot = {
        "week": week_key,
        "week_ending": week_ending,
        "pulled_at": until_s,
        "summary": "",
        "summary_source": "fallback",
        "sites": sites_out,
    }

    # Idempotent merge: one entry per ISO week, latest pull wins.
    weeks = [w for w in history["weeks"] if w["week"] != week_key]
    weeks.append(snapshot)
    weeks.sort(key=lambda w: w["week_ending"])
    prev_week = weeks[-2] if len(weeks) >= 2 else None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key and not MOCK:
        try:
            snapshot["summary"] = haiku_summary(api_key, snapshot, prev_week)
            snapshot["summary_source"] = "haiku"
        except Exception as e:
            log(f"WARN summary model call failed, using fallback: {e}")
            snapshot["summary"] = fallback_summary(snapshot, prev_week)
    else:
        snapshot["summary"] = fallback_summary(snapshot, prev_week)

    history["weeks"] = weeks
    history["updated_at"] = until_s

    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    atomic_write(HISTORY_PATH, json.dumps(history, indent=1) + "\n")
    report_path = os.path.join(REPORTS_DIR, f"traffic-{week_ending}.md")
    atomic_write(report_path, build_report(weeks))
    log(f"Wrote {HISTORY_PATH} ({len(weeks)} weeks) and {report_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
