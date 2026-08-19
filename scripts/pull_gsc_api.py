#!/usr/bin/env python3
"""Pull Search Console performance data via the API into the dashboard.

This is the automated replacement for the manual CSV export that import_gsc.py
handles. It reads a Google service-account key, lists every property the service
account can see, pulls the last N days of Search Analytics for each, and writes
the same data/search-console.json the dashboard already renders.

Setup (one time):
  1. Create a Google Cloud service account, enable the Search Console API,
     download its JSON key.
  2. Add the service account's email as a user on each GSC property.
  3. Put the key at .secrets/gsc-service-account.json (git-ignored), or point
     GSC_KEY at it.

Run:  python3 scripts/pull_gsc_api.py [days]      (default 90)

Nothing here writes to Search Console; it only reads. The key never leaves the
machine and .secrets/ is git-ignored.
"""
import os, sys, json, urllib.parse
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
import google.auth.transport.requests
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = os.environ.get("GSC_KEY") or os.path.join(ROOT, ".secrets", "gsc-service-account.json")
OUT = os.path.join(ROOT, "data", "search-console.json")
API = "https://searchconsole.googleapis.com/webmasters/v3"
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90


def log(m): print(m, flush=True)


def site_key(site_url):
    """Map a GSC property id to the dashboard's hostname key.
    sc-domain:gocheckmyparents.com -> gocheckmyparents.com
    https://gocheckmyparents.com/  -> gocheckmyparents.com
    """
    if site_url.startswith("sc-domain:"):
        return site_url[len("sc-domain:"):]
    return site_url.replace("https://", "").replace("http://", "").strip("/")


def main():
    if not os.path.exists(KEY):
        log("No key at %s. See the header of this script for setup." % KEY)
        sys.exit(1)

    creds = service_account.Credentials.from_service_account_file(KEY, scopes=SCOPES)
    creds.refresh(google.auth.transport.requests.Request())
    sess = requests.Session()
    sess.headers.update({"Authorization": "Bearer " + creds.token,
                         "Content-Type": "application/json"})

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=DAYS)
    s_start, s_end = start.isoformat(), end.isoformat()

    r = sess.get(API + "/sites", timeout=30)
    r.raise_for_status()
    sites = [s for s in r.json().get("siteEntry", [])
             if s.get("permissionLevel") != "siteUnverifiedUser"]
    log("service account can see %d propert%s" % (len(sites), "y" if len(sites) == 1 else "ies"))
    if not sites:
        log("Nothing to pull. Add the service-account email as a user on your GSC properties.")
        sys.exit(1)

    # preserve any sites already in the file (e.g. from a CSV import) that this
    # key can't see; overwrite the ones we can pull with fresh API data.
    out = {"sites": {}}
    if os.path.exists(OUT):
        try:
            out = json.load(open(OUT))
            out.setdefault("sites", {})
        except Exception:
            out = {"sites": {}}

    def query(site_url, dims, limit):
        body = {"startDate": s_start, "endDate": s_end,
                "dimensions": dims, "rowLimit": limit}
        u = API + "/sites/" + urllib.parse.quote(site_url, safe="") + "/searchAnalytics/query"
        rr = sess.post(u, json=body, timeout=60)
        rr.raise_for_status()
        return rr.json().get("rows", [])

    def rows_for(site_url, dim, limit):
        out_rows = []
        for row in query(site_url, [dim], limit):
            out_rows.append({"key": row["keys"][0],
                             "clicks": row.get("clicks", 0.0),
                             "impressions": row.get("impressions", 0.0),
                             "ctr": round(row.get("ctr", 0.0) * 100, 2),  # store as percent, matches CSV import
                             "position": round(row.get("position", 0.0), 2)})
        return out_rows

    for s in sites:
        su = s["siteUrl"]
        host = site_key(su)
        log("  pulling %s (%s) ..." % (host, su))
        dates = rows_for(su, "date", 500)
        dates.sort(key=lambda x: x["key"])
        queries = rows_for(su, "query", 500)
        pages = rows_for(su, "page", 500)
        countries = rows_for(su, "country", 50)
        devices = rows_for(su, "device", 10)
        for lst in (queries, pages, countries, devices):
            lst.sort(key=lambda x: -x["impressions"])

        clicks = sum(r["clicks"] for r in dates)
        impr = sum(r["impressions"] for r in dates)
        positions = [r["position"] for r in dates if r["impressions"] > 0]
        totals = {"clicks": round(clicks), "impressions": round(impr),
                  "ctr_pct": round(clicks / impr * 100, 2) if impr else None,
                  "avg_position": round(sum(positions) / len(positions), 1) if positions else None,
                  "basis": "dates"}

        out["sites"][host] = {
            "imported_from": "GSC API",
            "dates": dates, "queries": queries, "pages": pages,
            "countries": countries, "devices": devices,
            "totals": totals, "range": {"from": s_start, "to": s_end}}
        log("      %d impressions, %d clicks, avg position %s (%d queries, %d pages)" % (
            totals["impressions"], totals["clicks"], totals["avg_position"],
            len(queries), len(pages)))

    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    out["gsc_source"] = "api"
    json.dump(out, open(OUT, "w"), indent=1)
    log("\nWrote %s covering %d propert%s. Refresh the dashboard." % (
        OUT, len(sites), "y" if len(sites) == 1 else "ies"))


if __name__ == "__main__":
    main()
