#!/usr/bin/env python3
"""Import affiliate-network click and commission exports into the dashboard.

This is the money half of the weekly rollup. The traffic half already comes from
Cloudflare and Search Console; nothing there can see an outbound click, because
the click leaves for a network we do not control.

The whole thing works because of the subid convention. Every placement in the
family reports as:

    {site}_{page}__{placement}        e.g. parents_care-binder__paperwork

so a flat list of click references from a network export splits cleanly into
three dimensions with no lookup table and no per-site configuration.

CROSS-SITE HANDOFFS COME FROM THE SAME EXPORTS, which is the part worth
understanding. When a reader arrives from another family site, family.js keeps
the route label for the tab and the affiliate builders put it in the network's
SECOND slot: clickref2 on Awin, subId2 on Impact, and a joined value on
single-slot networks like CJ. So the network reports "this click came from a
reader that Storm sent to Pet" without any analytics package, any cookie, or any
custom event pipeline. The handoff count is a by-product of the money data.

Usage:
    1. Export clicks from each network:
         Awin    Reports > Transactions or Clickref report > Download CSV
         Impact  Reports > Action Listing or SubId performance > Export CSV
         CJ      Reports > Commission Detail or SID report > Export CSV
    2. Drop the CSVs anywhere under:
           data/affiliates/drop/
       Put the network name in the filename or in a subfolder named for it
       (awin, impact, cj). Anything else is read as "unknown" and still counted.
    3. Run:  python3 scripts/import_affiliates.py
       It writes data/affiliate-rollup.json, which rollup.html reads.

Re-running is safe: each network is replaced by its newest import, never
appended. Nothing here talks to the network and no credentials are needed, the
same posture as import_gsc.py.

Column names differ per network and change over time, so matching is done on
normalized header fragments rather than exact names, and anything unmatched is
reported rather than silently dropped.
"""

import csv
import io
import json
import os
import re
import sys
import zipfile
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DROP = os.path.join(ROOT, "data", "affiliates", "drop")
OUT = os.path.join(ROOT, "data", "affiliate-rollup.json")

NETWORKS = ("awin", "impact", "cj")

# Header fragments, checked against a lowercased header with non-letters removed.
# Order matters: the first match wins, so the more specific fragment goes first.
FIELDS = {
    "subid":  ["clickref1", "clickref", "subid1", "subid", "sid", "sourceid"],
    "origin": ["clickref2", "subid2", "sid2"],
    "clicks": ["clicks", "clickcount", "totalclicks"],
    "sales":  ["sales", "transactions", "actions", "conversions", "orders"],
    "money":  ["commission", "payout", "earnings", "publisherpayout", "revenue"],
}


def norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def pick(headers, key):
    """Index of the first header matching this field, or None."""
    normed = [norm(h) for h in headers]
    for frag in FIELDS[key]:
        for i, h in enumerate(normed):
            if h == frag:
                return i
    for frag in FIELDS[key]:                      # looser: contains
        for i, h in enumerate(normed):
            if frag in h:
                return i
    return None


def network_of(path):
    low = path.lower()
    for n in NETWORKS:
        if n in low:
            return n
    return "unknown"


def parse_subid(sub):
    """{site}_{page}__{placement} -> (site, page, placement).

    Returns (None, None, raw) for anything that does not fit, so pre-convention
    rows still show up in the report instead of vanishing. A joined value from a
    single-slot network (placement~origin) is split before parsing."""
    sub = (sub or "").strip()
    if not sub:
        return (None, None, "")
    joined_origin = None
    if "~" in sub:
        sub, joined_origin = sub.split("~", 1)
    m = re.match(r"^([a-z0-9]+)_([a-z0-9-]+)__([a-z0-9-]+)$", sub)
    if not m:
        return (None, None, sub, joined_origin)
    return (m.group(1), m.group(2), m.group(3), joined_origin)


def rows_from_csv(text):
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    r = csv.reader(io.StringIO(text), dialect)
    return [row for row in r if row]


def read_files():
    """Yield (network, filename, rows). Handles bare CSVs and zips."""
    if not os.path.isdir(DROP):
        return
    for base, _dirs, files in os.walk(DROP):
        for fn in sorted(files):
            path = os.path.join(base, fn)
            rel = os.path.relpath(path, DROP)
            net = network_of(rel)
            if fn.lower().endswith(".zip"):
                try:
                    with zipfile.ZipFile(path) as z:
                        for inner in z.namelist():
                            if not inner.lower().endswith(".csv"):
                                continue
                            with z.open(inner) as fh:
                                yield net, rel + ":" + inner, rows_from_csv(
                                    fh.read().decode("utf-8", "replace"))
                except zipfile.BadZipFile:
                    print("  skipped (not a readable zip): " + rel)
            elif fn.lower().endswith(".csv"):
                with io.open(path, encoding="utf-8", errors="replace") as fh:
                    yield net, rel, rows_from_csv(fh.read())


def num(v):
    if v is None:
        return 0.0
    s = re.sub(r"[^0-9.\-]", "", str(v))
    try:
        return float(s) if s not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def main():
    placements = defaultdict(lambda: {"clicks": 0.0, "sales": 0.0, "money": 0.0})
    handoffs = defaultdict(lambda: {"clicks": 0.0, "sales": 0.0, "money": 0.0})
    per_network = defaultdict(lambda: {"rows": 0, "clicks": 0.0, "money": 0.0})
    unparsed = defaultdict(float)
    files_read = []
    skipped = []

    for net, name, rows in read_files():
        if not rows:
            continue
        headers = rows[0]
        i_sub = pick(headers, "subid")
        if i_sub is None:
            skipped.append({"file": name, "why": "no click-reference column found",
                            "headers": headers[:12]})
            continue
        i_org = pick(headers, "origin")
        i_clk = pick(headers, "clicks")
        i_sal = pick(headers, "sales")
        i_mon = pick(headers, "money")
        files_read.append({"file": name, "network": net, "rows": len(rows) - 1})

        for row in rows[1:]:
            def cell(i):
                return row[i] if (i is not None and i < len(row)) else None
            parsed = parse_subid(cell(i_sub))
            site, page, placement = parsed[0], parsed[1], parsed[2]
            joined_origin = parsed[3] if len(parsed) > 3 else None
            origin = (cell(i_org) or joined_origin or "").strip()

            # A network that reports one row per click has no clicks column.
            clicks = num(cell(i_clk)) or (1.0 if i_clk is None else 0.0)
            sales = num(cell(i_sal))
            money = num(cell(i_mon))

            per_network[net]["rows"] += 1
            per_network[net]["clicks"] += clicks
            per_network[net]["money"] += money

            if site is None:
                if placement:
                    unparsed[placement] += clicks
                continue

            key = "%s|%s|%s|%s" % (site, page, placement, net)
            p = placements[key]
            p["clicks"] += clicks
            p["sales"] += sales
            p["money"] += money

            if origin:
                o = parse_subid(origin)
                from_site = o[0] or "unknown"
                if from_site != site:            # a genuine cross-site handoff
                    h = handoffs["%s|%s|%s" % (from_site, site, origin)]
                    h["clicks"] += clicks
                    h["sales"] += sales
                    h["money"] += money

    def rows_out(d, fields):
        out = []
        for key, v in sorted(d.items(), key=lambda kv: -kv[1]["clicks"]):
            parts = key.split("|")
            row = dict(zip(fields, parts))
            row.update({k: round(x, 2) for k, x in v.items()})
            out.append(row)
        return out

    data = {
        "schema": 1,
        "generated_from": "manual network exports in data/affiliates/drop/",
        "files_read": files_read,
        "skipped_files": skipped,
        "networks": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv
                         for kk, vv in v.items()}
                     for k, v in per_network.items()},
        "placements": rows_out(placements, ["site", "page", "placement", "network"]),
        "handoffs": rows_out(handoffs, ["from_site", "to_site", "origin"]),
        "unparsed_subids": [{"value": k, "clicks": round(v, 2)}
                            for k, v in sorted(unparsed.items(), key=lambda kv: -kv[1])],
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print("read %d file(s) from data/affiliates/drop/" % len(files_read))
    for s in skipped:
        print("  SKIPPED %s: %s" % (s["file"], s["why"]))
    print("placements: %d" % len(data["placements"]))
    print("cross-site handoffs: %d" % len(data["handoffs"]))
    if data["unparsed_subids"]:
        print("subids not matching {site}_{page}__{placement}: %d (listed in the JSON)"
              % len(data["unparsed_subids"]))
    print("wrote " + os.path.relpath(OUT, ROOT))
    if not files_read:
        print("\nNothing to read yet. Export from Awin, Impact, and CJ and drop the")
        print("CSVs in data/affiliates/drop/, then run this again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
