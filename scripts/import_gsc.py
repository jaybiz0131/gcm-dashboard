#!/usr/bin/env python3
"""Import Google Search Console exports into the dashboard.

Search Console answers what Cloudflare structurally cannot: how many people
were SHOWN a page and chose not to click. Impressions and average position
are the only early signals a pre-traction site has, because they move months
before clicks do.

Usage:
    1. In Search Console pick a property, open Performance, set the date range,
       and press Export > Download CSV. You get a zip per property.
    2. Drop the zips (or unzipped folders) anywhere under:
           data/search-console/drop/
       Filenames normally contain the property, e.g.
       "gocheckmypet.com-Performance-on-Search-2026-07-31.zip". If a file does
       not name the property, put it in a subfolder named for the site instead.
    3. Run:  python3 scripts/import_gsc.py
       It writes data/search-console.json, which the dashboard reads.

Re-running is safe: each property is replaced by its newest import, never
duplicated. Nothing here talks to the network, and no credentials are needed.
"""

import csv
import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timezone

SITES = [
    "gocheckmy.com", "gocheckmyhome.com", "gocheckmypet.com",
    "gocheckmymortgage.com", "gocheckmyparents.com", "gocheckmyestate.com",
    "gocheckmystorm.com", "gocheckmycrypto.com", "gocheckmysports.com",
    "gocheckmynews.com",
]

ROOT = os.environ.get("GCM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DROP_DIR = os.path.join(ROOT, "data", "search-console", "drop")
OUT_PATH = os.path.join(ROOT, "data", "search-console.json")

# Search Console localises the first column header but keeps the metric names,
# so sheets are identified by their key column rather than by filename.
SHEET_KEYS = {
    "query": "queries", "queries": "queries", "top queries": "queries",
    "page": "pages", "pages": "pages", "top pages": "pages",
    "date": "dates",
    "country": "countries",
    "device": "devices",
}


def log(m):
    print(m, flush=True)


def site_for(path):
    """Longest matching hostname wins: gocheckmy.com is a substring of the rest."""
    low = path.lower()
    best = None
    for s in SITES:
        if s in low and (best is None or len(s) > len(best)):
            best = s
    return best


def num(v):
    if v is None:
        return None
    v = str(v).strip().replace(",", "").replace("%", "")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_csv(text):
    """Return (kind, rows) for one Search Console CSV, or (None, []) if unknown."""
    try:
        reader = csv.reader(io.StringIO(text))
        header = next(reader)
    except (StopIteration, csv.Error):
        return None, []
    if not header:
        return None, []
    key = (header[0] or "").strip().lower().lstrip("﻿")
    kind = SHEET_KEYS.get(key)
    if not kind:
        return None, []
    cols = {(h or "").strip().lower(): i for i, h in enumerate(header)}

    def pick(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    i_clicks = pick("clicks", "url clicks")
    i_impr = pick("impressions")
    i_ctr = pick("ctr", "site ctr", "url ctr")
    i_pos = pick("position", "average position")
    rows = []
    for r in reader:
        if not r or not (r[0] or "").strip():
            continue
        rows.append({
            "key": r[0].strip(),
            "clicks": num(r[i_clicks]) if i_clicks is not None and i_clicks < len(r) else None,
            "impressions": num(r[i_impr]) if i_impr is not None and i_impr < len(r) else None,
            "ctr": num(r[i_ctr]) if i_ctr is not None and i_ctr < len(r) else None,
            "position": num(r[i_pos]) if i_pos is not None and i_pos < len(r) else None,
        })
    return kind, rows


def read_sources(path):
    """Yield (label, text) for every CSV in a file, transparently unzipping."""
    if path.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if name.lower().endswith(".csv"):
                        yield f"{os.path.basename(path)}:{name}", z.read(name).decode("utf-8-sig", "replace")
        except zipfile.BadZipFile:
            log(f"  ! {os.path.basename(path)} is not a readable zip, skipped")
    elif path.lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            yield os.path.basename(path), f.read()


def main():
    if not os.path.isdir(DROP_DIR):
        os.makedirs(DROP_DIR, exist_ok=True)
        log(f"Created {DROP_DIR}\nDrop your Search Console exports there and run this again.")
        return 0

    found = []
    for dirpath, _dirs, files in os.walk(DROP_DIR):
        for fn in files:
            if fn.lower().endswith((".zip", ".csv")) and not fn.startswith("."):
                found.append(os.path.join(dirpath, fn))
    if not found:
        log(f"No .zip or .csv files under {DROP_DIR}.\n"
            f"Search Console > Performance > Export > Download CSV, then drop the file there.")
        return 0

    out = {}
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH) as f:
                out = json.load(f).get("sites", {}) or {}
        except (OSError, json.JSONDecodeError):
            out = {}

    unmatched = []
    for path in sorted(found):
        rel = os.path.relpath(path, DROP_DIR)
        site = site_for(rel) or site_for(path)
        if not site:
            unmatched.append(rel)
            continue
        entry = {"imported_from": os.path.basename(path)}
        for label, text in read_sources(path):
            kind, rows = parse_csv(text)
            if not kind:
                continue
            if kind in ("queries", "pages", "countries", "devices"):
                rows.sort(key=lambda r: -(r["impressions"] or 0))
                entry[kind] = rows[:50]
            elif kind == "dates":
                rows.sort(key=lambda r: r["key"])
                entry["dates"] = rows
        if len(entry) == 1:
            log(f"  ? {rel}: no recognisable Search Console sheets inside")
            continue

        # Totals come from the date sheet when present because it is the only
        # one that is not truncated to a top-N list.
        base = entry.get("dates") or entry.get("queries") or []
        clicks = sum(r["clicks"] or 0 for r in base)
        impr = sum(r["impressions"] or 0 for r in base)
        positions = [r["position"] for r in base if r["position"] is not None]
        entry["totals"] = {
            "clicks": round(clicks),
            "impressions": round(impr),
            "ctr_pct": round(clicks / impr * 100, 2) if impr else None,
            "avg_position": round(sum(positions)/len(positions), 1) if positions else None,
            "basis": "dates" if entry.get("dates") else "queries (top rows only)",
        }
        if entry.get("dates"):
            entry["range"] = {"from": entry["dates"][0]["key"], "to": entry["dates"][-1]["key"]}
        out[site] = entry
        t = entry["totals"]
        log(f"  + {site}: {t['impressions']} impressions, {t['clicks']} clicks, "
            f"avg position {t['avg_position']} ({rel})")

    for u in unmatched:
        log(f"  ! could not tell which property '{u}' belongs to. Rename it to include "
            f"the hostname, or move it into a subfolder named for the site.")

    if not out:
        log("Nothing imported.")
        return 1
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sites": out,
    }
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(payload, indent=1) + "\n")
    json.loads(open(tmp).read())
    os.replace(tmp, OUT_PATH)
    log(f"\nWrote {OUT_PATH} covering {len(out)} propert{'y' if len(out)==1 else 'ies'}.")
    log("Refresh the dashboard to see the Search visibility section.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
