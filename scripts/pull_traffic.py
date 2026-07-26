#!/usr/bin/env python3
"""GCM traffic + experience pull.

Every run (daily cron): pulls the last 28 complete days of Cloudflare Web
Analytics per property (per-day visits, pageviews, device split), plus
real-user Core Web Vitals p75 (LCP / INP / CLS, overall and by device),
upserts both into data/traffic-history.json, and recomputes regression
alerts into data/alerts-latest.json.

Sundays only (or GCM_FORCE_WEEKLY=1): additionally builds the weekly
snapshot (visits, top paths, Haiku summary), writes
reports/traffic-YYYY-MM-DD.md, and runs a Lighthouse lab pass per origin
using the lighthouse CLI (performance + accessibility scores). The CLI
needs node + Chrome, which GitHub runners have preinstalled; locally it
is skipped gracefully if npx is missing.

Stdlib only. Secrets come from the environment:
  CLOUDFLARE_ANALYTICS_TOKEN  read-only Analytics token (required)
  ANTHROPIC_API_KEY           for the weekly Haiku summary (optional;
                              a deterministic fallback summary is used
                              if absent or the call fails)

Local testing:
  GCM_MOCK=1     deterministic fake data, no network at all
  GCM_ROOT=dir   write data/ and reports/ under dir instead of the repo
  GCM_NOW=date   pretend "now" is this ISO date (mock/testing only)
  GCM_FORCE_WEEKLY=1  run the Sunday-only work regardless of weekday
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
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
    "gocheckmysports.com",
    "gocheckmynews.com",
]

CF_API = "https://api.cloudflare.com/client/v4"
CF_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

ET = ZoneInfo("America/New_York")

DAILY_WINDOW_DAYS = 28
VITALS_MIN_SAMPLES = 10   # below this, p75 is noise: shown but never alerted on
A11Y_ALERT_FLOOR = 90

# Core Web Vitals thresholds (Google's good / poor boundaries)
VITAL_GOOD = {"lcp_p75_ms": 2500, "inp_p75_ms": 200, "cls_p75": 0.1}
VITAL_POOR = {"lcp_p75_ms": 4000, "inp_p75_ms": 500, "cls_p75": 0.25}

ROOT = os.environ.get("GCM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_PATH = os.path.join(ROOT, "data", "traffic-history.json")
ALERTS_PATH = os.path.join(ROOT, "data", "alerts-latest.json")
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


def host_filter(site_tag, host, since, until):
    return (
        f'{{siteTag: "{site_tag}", requestHost_in: ["{host}", "www.{host}"], '
        f'datetime_geq: "{since}", datetime_lt: "{until}"}}'
    )


def graphql(token, query):
    resp = http_json(CF_GRAPHQL, method="POST", headers=cf_headers(token), body={"query": query})
    if resp.get("errors"):
        raise RuntimeError(f"GraphQL error: {resp['errors']}")
    accounts = (resp.get("data") or {}).get("viewer", {}).get("accounts") or []
    if not accounts:
        raise RuntimeError("GraphQL returned no account data")
    return accounts[0]


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
    flt = host_filter(site_tag, host, since, until)
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
    acct = graphql(token, query)
    totals = acct.get("totals") or []
    visits = totals[0]["sum"]["visits"] if totals else 0
    pageviews = totals[0]["count"] if totals else 0
    top_paths = [
        {"path": g["dimensions"]["requestPath"] or "/", "pageviews": g["count"]}
        for g in (acct.get("topPaths") or [])
        if g.get("dimensions")
    ][:3]
    return {"visits": visits, "pageviews": pageviews, "top_paths": top_paths, "no_data": False}


def pull_daily(token, account_tag, site_tag, host, since, until):
    """Per-day visits, pageviews, and device split for one property."""
    flt = host_filter(site_tag, host, since, until)
    query = f"""
    {{
      viewer {{
        accounts(filter: {{accountTag: "{account_tag}"}}) {{
          byDay: rumPageloadEventsAdaptiveGroups(filter: {flt}, limit: 200) {{
            count
            sum {{ visits }}
            dimensions {{ date deviceType }}
          }}
        }}
      }}
    }}
    """
    acct = graphql(token, query)
    days = {}
    for g in acct.get("byDay") or []:
        dims = g.get("dimensions") or {}
        d = dims.get("date")
        if not d:
            continue
        dev = (dims.get("deviceType") or "").lower()
        rec = days.setdefault(d, {"visits": 0, "pageviews": 0,
                                  "mobile_visits": 0, "desktop_visits": 0})
        rec["visits"] += g["sum"]["visits"]
        rec["pageviews"] += g["count"]
        if dev == "mobile":
            rec["mobile_visits"] += g["sum"]["visits"]
        elif dev == "desktop":
            rec["desktop_visits"] += g["sum"]["visits"]
    return days


VITALS_FIELDS = ["largestContentfulPaintP75", "interactionToNextPaintP75",
                 "cumulativeLayoutShiftP75"]


def _vitals_record(group, fields):
    # Cloudflare returns the timing quantiles in MICROSECONDS (verified against
    # live data 2026-07-26: healthy static pages came back as e.g. 1155199).
    # CLS is unitless and needs no conversion.
    qt = (group or {}).get("quantiles") or {}
    lcp = qt.get("largestContentfulPaintP75")
    inp = qt.get("interactionToNextPaintP75")
    cls = qt.get("cumulativeLayoutShiftP75")
    return {
        "lcp_p75_ms": round(lcp / 1000) if lcp is not None else None,
        "inp_p75_ms": round(inp / 1000) if (inp is not None and "interactionToNextPaintP75" in fields) else None,
        "cls_p75": round(cls, 3) if cls is not None else None,
        "samples": (group or {}).get("count", 0),
    }


def pull_vitals(token, account_tag, site_tag, host, since, until):
    """Real-user Core Web Vitals p75 over the window, overall and by device.

    The INP quantile is newer than the rest of the schema; if this zone's
    GraphQL rejects the field, retry without it rather than losing LCP/CLS.
    """
    flt = host_filter(site_tag, host, since, until)
    fields = list(VITALS_FIELDS)
    while True:
        qfields = " ".join(fields)
        query = f"""
        {{
          viewer {{
            accounts(filter: {{accountTag: "{account_tag}"}}) {{
              overall: rumWebVitalsEventsAdaptiveGroups(filter: {flt}, limit: 1) {{
                count
                quantiles {{ {qfields} }}
              }}
              byDevice: rumWebVitalsEventsAdaptiveGroups(filter: {flt}, limit: 10) {{
                count
                quantiles {{ {qfields} }}
                dimensions {{ deviceType }}
              }}
            }}
          }}
        }}
        """
        try:
            acct = graphql(token, query)
            break
        except RuntimeError as e:
            if "interactionToNextPaintP75" in fields and "interactionToNextPaint" in str(e):
                fields.remove("interactionToNextPaintP75")
                continue
            raise
    overall = (acct.get("overall") or [None])[0]
    out = {"overall": _vitals_record(overall, fields)}
    for g in acct.get("byDevice") or []:
        dev = ((g.get("dimensions") or {}).get("deviceType") or "").lower()
        if dev in ("mobile", "desktop"):
            out[dev] = _vitals_record(g, fields)
    return out


# ---------------------------------------------------------------- Lighthouse

def pull_lighthouse(host):
    """One mobile Lighthouse lab run via the lighthouse CLI.

    Runs where node + Chrome exist (GitHub runners have both preinstalled);
    anywhere else the FileNotFoundError is caught by the caller and the lab
    layer is simply skipped. No API key, no external quota.
    """
    import subprocess
    import tempfile
    out = os.path.join(tempfile.gettempdir(), f"lh-{host}.json")
    subprocess.run(
        ["npx", "--yes", "lighthouse@12", f"https://{host}/",
         "--only-categories=performance,accessibility",
         "--output=json", f"--output-path={out}", "--quiet",
         "--chrome-flags=--headless=new --no-sandbox"],
        check=True, timeout=300, capture_output=True)
    with open(out) as f:
        lr = json.load(f)
    cats = lr.get("categories") or {}
    audits = lr.get("audits") or {}

    def score(name):
        s = (cats.get(name) or {}).get("score")
        return round(s * 100) if s is not None else None

    def num(name):
        return (audits.get(name) or {}).get("numericValue")

    lcp, cls, tbt = num("largest-contentful-paint"), num("cumulative-layout-shift"), num("total-blocking-time")
    return {
        "performance": score("performance"),
        "accessibility": score("accessibility"),
        "lab_lcp_ms": round(lcp) if lcp is not None else None,
        "lab_cls": round(cls, 3) if cls is not None else None,
        "lab_tbt_ms": round(tbt) if tbt is not None else None,
    }


# ---------------------------------------------------------------- mock mode

def _seed(*parts):
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


def mock_pull(site, week_key):
    """Deterministic fake numbers so idempotency tests are meaningful."""
    seed = _seed(site, week_key)
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


def mock_daily(site, day_list):
    days = {}
    for d in day_list:
        seed = _seed(site, d)
        v = 2 + seed % 18
        if site in ("gocheckmysports.com", "gocheckmynews.com") and d < "2026-07-19":
            continue  # went live mid-window
        m = int(v * (0.4 + (seed % 4) / 10))
        days[d] = {"visits": v, "pageviews": int(v * 1.5), "mobile_visits": m,
                   "desktop_visits": v - m}
    return days


def mock_vitals(site):
    seed = _seed(site, "vitals")
    if site == "gocheckmycrypto.com":  # exercise the poor/alert path
        base = {"lcp_p75_ms": 4400, "inp_p75_ms": 520, "cls_p75": 0.02, "samples": 40}
    else:
        base = {"lcp_p75_ms": 1400 + seed % 1400, "inp_p75_ms": 80 + seed % 160,
                "cls_p75": round((seed % 12) / 100, 3), "samples": 15 + seed % 60}
    mob = dict(base, lcp_p75_ms=(base["lcp_p75_ms"] or 0) + 300,
               samples=max(1, base["samples"] // 2))
    return {"overall": base, "mobile": mob, "desktop": dict(base, samples=base["samples"] // 3)}


def mock_psi(site):
    seed = _seed(site, "psi")
    a11y = 88 if site == "gocheckmypet.com" else 94 + seed % 7  # one amber to exercise coloring
    return {"performance": 70 + seed % 28, "accessibility": a11y,
            "lab_lcp_ms": 1800 + seed % 1500, "lab_cls": round((seed % 9) / 100, 3),
            "lab_tbt_ms": 40 + seed % 300}


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
    # Totals and the headline delta are computed here, not by the model:
    # a summary that misstates the total is worse than no summary.
    total = sum(s["visits"] for s in week["sites"].values() if not s.get("no_data"))
    prev_total = None
    if prev_week:
        prev_total = sum(s["visits"] for s in prev_week["sites"].values() if not s.get("no_data"))
    payload = {
        "this_week": {h: {"visits": s["visits"], "pageviews": s["pageviews"],
                          "top_path": (s["top_paths"][0]["path"] if s["top_paths"] else None),
                          "no_data": s.get("no_data", False)}
                      for h, s in week["sites"].items()},
        "last_week": {h: s["visits"] for h, s in (prev_week or {}).get("sites", {}).items()} or None,
        "computed": {"this_week_total": total, "last_week_total": prev_total,
                     "total_wow_pct": fmt_pct(total, prev_total)},
        "week_ending": week["week_ending"],
    }
    prompt = (
        "You write a one-breath weekly traffic note for the owner of a small family of websites. "
        "Here is this week's data (visits are the trailing 7 days):\n\n"
        + json.dumps(payload, indent=1)
        + "\n\nWrite 2-3 plain-English sentences: what moved and by roughly how much, any site "
        "that got its first traffic, anything notable. Total and biggest changes first. "
        "Use the numbers in \"computed\" for the total and week-over-week change verbatim; "
        "do not do your own arithmetic on totals. "
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


# ---------------------------------------------------------------- alerts

def vital_status(metric, value):
    if value is None:
        return None
    if value <= VITAL_GOOD[metric]:
        return "good"
    if value <= VITAL_POOR[metric]:
        return "ni"
    return "poor"


def compute_alerts(history):
    """Newly-poor Core Web Vitals and accessibility regressions.

    Transition-based on purpose: a metric alerts the day it turns poor, then
    goes quiet while it stays poor, so the daily cron never spams the repo
    with one open issue per day for the same regression.
    """
    alerts = []
    vit = history.get("vitals") or []
    cur, prev = (vit[-1] if vit else None), (vit[-2] if len(vit) >= 2 else None)
    if cur:
        for host in SITES:
            c = ((cur.get("sites") or {}).get(host) or {}).get("overall") or {}
            if c.get("samples", 0) < VITALS_MIN_SAMPLES:
                continue
            p = (((prev or {}).get("sites") or {}).get(host) or {}).get("overall") or {}
            for metric, label, unit in (("lcp_p75_ms", "LCP", "ms"),
                                        ("inp_p75_ms", "INP", "ms"),
                                        ("cls_p75", "CLS", "")):
                if vital_status(metric, c.get(metric)) == "poor" and \
                   vital_status(metric, p.get(metric)) != "poor":
                    alerts.append(f"{host}: real-user {label} p75 is {c[metric]}{unit}, "
                                  f"past the poor threshold ({VITAL_POOR[metric]}{unit}).")
    lab = history.get("lab") or []
    cur_lab, prev_lab = (lab[-1] if lab else None), (lab[-2] if len(lab) >= 2 else None)
    if cur_lab:
        for host in SITES:
            c = ((cur_lab.get("sites") or {}).get(host) or {})
            p = (((prev_lab or {}).get("sites") or {}).get(host) or {})
            a, pa = c.get("accessibility"), p.get("accessibility")
            if a is not None and a < A11Y_ALERT_FLOOR and (pa is None or pa >= A11Y_ALERT_FLOOR):
                alerts.append(f"{host}: Lighthouse accessibility score dropped to {a} "
                              f"(alert floor {A11Y_ALERT_FLOOR}).")
    return alerts


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


def daterange(first, last_exclusive):
    d, out = first, []
    while d < last_exclusive:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def main():
    now = now_et()
    until = now.astimezone(timezone.utc)
    since7 = until - timedelta(days=7)
    iso = now.isocalendar()
    week_key = f"{iso.year}-W{iso.week:02d}"
    week_ending = now.date().isoformat()
    since7_s = since7.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_s = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Daily series and vitals run over complete UTC days only, so every run
    # of a given day writes identical values (idempotent, and the charts
    # never show a half-day dip at the right edge).
    day_end = until.date()                       # today, exclusive
    day_start = day_end - timedelta(days=DAILY_WINDOW_DAYS)
    dstart_s = f"{day_start.isoformat()}T00:00:00Z"
    dend_s = f"{day_end.isoformat()}T00:00:00Z"
    window_days = daterange(day_start, day_end)

    do_weekly = MOCK or now.weekday() == 6 or os.environ.get("GCM_FORCE_WEEKLY") == "1"
    log(f"Daily window {dstart_s} to {dend_s}; weekly={'yes' if do_weekly else 'no'} "
        f"(week {week_key}, ending {week_ending})")

    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            history = json.load(f)
    else:
        history = {"weeks": []}
    history["schema"] = 2
    history.setdefault("weeks", [])
    history.setdefault("days", [])
    history.setdefault("vitals", [])
    history.setdefault("lab", [])

    token = None
    mapping = {}
    if MOCK:
        log("MOCK mode: no network calls will be made.")
    else:
        token = os.environ.get("CLOUDFLARE_ANALYTICS_TOKEN")
        if not token:
            raise RuntimeError("CLOUDFLARE_ANALYTICS_TOKEN is not set")
        mapping = discover_sites(token, dstart_s, dend_s)
        log(f"Discovered {len(mapping)} Web Analytics properties: {sorted(mapping)}")

    # ---- daily series + vitals, every run
    daily_out, vitals_out, failures = {}, {}, 0
    for host in SITES:
        try:
            if MOCK:
                daily_out[host] = mock_daily(host, window_days)
                vitals_out[host] = mock_vitals(host)
            elif host in mapping:
                acct, tag = mapping[host]
                daily_out[host] = pull_daily(token, acct, tag, host, dstart_s, dend_s)
                vitals_out[host] = pull_vitals(token, acct, tag, host, dstart_s, dend_s)
            else:
                log(f"  {host}: not discovered, leaving existing daily history untouched")
                continue
            v = (vitals_out.get(host) or {}).get("overall") or {}
            log(f"  {host}: {sum(d['visits'] for d in daily_out[host].values())} visits/{DAILY_WINDOW_DAYS}d, "
                f"LCP p75 {v.get('lcp_p75_ms')}ms ({v.get('samples', 0)} samples)")
        except Exception as e:
            failures += 1
            log(f"WARN {host}: {e}")
    if not MOCK and failures == len(mapping) and mapping:
        raise RuntimeError("every discovered property failed to pull; refusing to commit")

    # Upsert per (date, site); a site that failed today keeps yesterday's rows.
    by_date = {d["date"]: d for d in history["days"]}
    for host, daymap in daily_out.items():
        for d in window_days:
            entry = by_date.setdefault(d, {"date": d, "sites": {}})
            entry["sites"][host] = daymap.get(d) or {"visits": 0, "pageviews": 0,
                                                     "mobile_visits": 0, "desktop_visits": 0}
    history["days"] = sorted(by_date.values(), key=lambda e: e["date"])[-400:]

    if vitals_out:
        vitals_entry = {"date": window_days[-1], "window_days": DAILY_WINDOW_DAYS,
                        "sites": vitals_out}
        history["vitals"] = ([v for v in history["vitals"] if v["date"] != vitals_entry["date"]]
                             + [vitals_entry])
        history["vitals"].sort(key=lambda v: v["date"])
        history["vitals"] = history["vitals"][-90:]

    # ---- Sunday-only work: weekly snapshot, report, Lighthouse lab pass
    report_written = None
    if do_weekly:
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
                    sites_out[host] = pull_site(token, acct, tag, host, since7_s, until_s)
            except Exception as e:
                log(f"WARN {host}: {e}")
                sites_out[host] = {"visits": 0, "pageviews": 0, "top_paths": [], "no_data": True,
                                   "note": f"pull failed: {e}"[:300]}

        # A quiet site is fine; every site failing means the API or token is broken.
        if all(s.get("no_data") for s in sites_out.values()) and \
           any("pull failed" in s.get("note", "") for s in sites_out.values()):
            raise RuntimeError("every property failed the weekly pull; refusing to commit an all-dark week")

        snapshot = {
            "week": week_key,
            "week_ending": week_ending,
            "pulled_at": until_s,
            "summary": "",
            "summary_source": "fallback",
            "sites": sites_out,
        }
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
        report_written = (f"traffic-{week_ending}.md", build_report(weeks))

        # Lighthouse lab pass, one mobile run per origin, fail-soft per site.
        lab_sites = {}
        for host in SITES:
            try:
                lab_sites[host] = mock_psi(host) if MOCK else pull_lighthouse(host)
                log(f"  lab {host}: perf {lab_sites[host]['performance']}, "
                    f"a11y {lab_sites[host]['accessibility']}")
            except FileNotFoundError:
                log("WARN lab: npx not available here, skipping the Lighthouse pass")
                break
            except Exception as e:
                log(f"WARN lab {host}: {e}")
        if lab_sites:
            lab_entry = {"date": week_ending, "sites": lab_sites}
            history["lab"] = ([l for l in history["lab"] if l["date"] != lab_entry["date"]]
                              + [lab_entry])
            history["lab"].sort(key=lambda l: l["date"])
            history["lab"] = history["lab"][-52:]

    alerts = compute_alerts(history)
    for a in alerts:
        log(f"ALERT: {a}")

    history["updated_at"] = until_s
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    atomic_write(HISTORY_PATH, json.dumps(history, indent=1) + "\n")
    atomic_write(ALERTS_PATH, json.dumps(
        {"generated_at": until_s, "alerts": alerts}, indent=1) + "\n")
    if report_written:
        atomic_write(os.path.join(REPORTS_DIR, report_written[0]), report_written[1])
    log(f"Wrote {HISTORY_PATH} ({len(history['days'])} days, {len(history['weeks'])} weeks, "
        f"{len(history['vitals'])} vitals entries, {len(history['lab'])} lab entries)"
        + (f" and reports/{report_written[0]}" if report_written else ""))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
