#!/usr/bin/env python3
"""Rebuild marketer_reports/visit_history_data.json for the Field Visit History report.

The SPOTIO era (2022-10 through 2026-09-17) is a one-time, frozen backfill --
that data lives in visit_history_base.json and never changes, so it's loaded
as-is rather than re-derived from HubSpot every run.

The MMC era (2026-09-18 onward) is live and growing, so this script re-fetches
ALL MMC-sourced Meeting engagements since that cutoff on every run (cheap --
low thousands of records, nowhere near HubSpot's 10k search-pagination cap)
and merges them onto the frozen base, extending the rep/territory/result/
company dictionaries for anything new (a newly hired rep, a newly visited
office) along the way.

Requires HUBSPOT_TOKEN in the environment.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_FILE = REPO_ROOT / "marketer_reports" / "visit_history_base.json"
BASE_NOTES_FILE = REPO_ROOT / "marketer_reports" / "visit_history_base_notes.json"
OUT_DIR = REPO_ROOT / "out" / "marketer_reports"
OUT_FILE = OUT_DIR / "visit_history_data.json"
OUT_NOTES_FILE = OUT_DIR / "visit_history_notes.json"
# Cloudflare Pages rejects any single file over 25 MiB. Notes text alone
# pushes a combined file well past that, so it ships as a second file,
# index-aligned with OUT_FILE's rows (same order, zero gaps).

HS_BASE = "https://api.hubapi.com"
TOKEN = os.environ["HUBSPOT_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
MMC_SOURCE_ID = "211206"
CUTOFF = datetime(2026, 9, 18, tzinfo=timezone.utc)
ZIP_RE = re.compile(r"\b(\d{5})\b")
# Same patterns phi_scan.py's email/phone rules use -- reps occasionally jot
# down a referring office's contact info in a check-in note, which the PHI
# gate (correctly) blocks on. Redact rather than suppress the gate.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")


def redact_contact_info(text):
    text = EMAIL_RE.sub("[redacted]", text or "")
    text = PHONE_RE.sub("[redacted]", text)
    return text


def hs_post(path, body, attempts=5):
    for i in range(attempts):
        r = requests.post(f"{HS_BASE}{path}", headers=HEADERS, json=body, timeout=30)
        if r.status_code == 429 or 500 <= r.status_code < 600:
            time.sleep(2 ** i)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def hs_get(path, attempts=5):
    for i in range(attempts):
        r = requests.get(f"{HS_BASE}{path}", headers=HEADERS, timeout=30)
        if r.status_code == 429 or 500 <= r.status_code < 600:
            time.sleep(2 ** i)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def normalize_name(name):
    name = (name or "").strip()
    if "," in name:
        last, first = [p.strip() for p in name.split(",", 1)]
        return f"{first} {last}".strip()
    return name


def parse_ts(ts):
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
    except (ValueError, TypeError):
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def fetch_owners():
    owners, after = {}, None
    while True:
        params = "?limit=100" + (f"&after={after}" if after else "")
        r = hs_get(f"/crm/v3/owners{params}")
        for o in r.get("results", []):
            name = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip() or o.get("email", "")
            owners[str(o["id"])] = name
        after = (r.get("paging", {}) or {}).get("next", {}).get("after")
        if not after:
            break
    return owners


def fetch_mmc_meetings():
    since_ms = int(CUTOFF.timestamp() * 1000)
    meetings, after = [], None
    while True:
        body = {
            "limit": 100,
            "properties": ["hs_meeting_start_time", "hs_meeting_title", "hubspot_owner_id", "hs_meeting_body"],
            "filterGroups": [{"filters": [
                {"propertyName": "hs_object_source_id", "operator": "EQ", "value": MMC_SOURCE_ID},
                {"propertyName": "hs_meeting_start_time", "operator": "GTE", "value": since_ms},
            ]}],
        }
        if after:
            body["after"] = after
        r = hs_post("/crm/v3/objects/meetings/search", body)
        meetings.extend(r.get("results", []))
        after = (r.get("paging", {}) or {}).get("next", {}).get("after")
        if not after:
            break
    return meetings


def batch_meeting_company_assoc(meeting_ids):
    m2c = {}
    for i in range(0, len(meeting_ids), 100):
        batch = meeting_ids[i:i + 100]
        rr = hs_post("/crm/v4/associations/meetings/companies/batch/read",
                     {"inputs": [{"id": x} for x in batch]})
        for row in rr.get("results", []):
            tos = row.get("to", [])
            if tos:
                m2c[str(row["from"]["id"])] = str(tos[0]["toObjectId"])
    return m2c


def batch_companies(company_ids):
    companies = {}
    ids = sorted(set(company_ids))
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        rr = hs_post("/crm/v3/objects/companies/batch/read", {
            "inputs": [{"id": x} for x in batch],
            "properties": ["name", "market2", "address", "hubspot_owner_id"],
        })
        for c in rr.get("results", []):
            companies[c["id"]] = c["properties"]
    return companies


def main():
    base = json.loads(BASE_FILE.read_text())
    reps = list(base["reps"])
    territories = list(base["territories"])
    results = list(base["results"])
    companies_list = list(base["companies"])
    addresses = list(base["addresses"])
    zips = list(base["zips"])
    base_notes = json.loads(BASE_NOTES_FILE.read_text())
    # Keep each row paired with its note so a later sort can't desync them.
    rows = [list(r) + [n] for r, n in zip(base["rows"], base_notes)]

    rep_idx_map = {v: i for i, v in enumerate(reps)}
    terr_idx_map = {v: i for i, v in enumerate(territories)}
    result_idx_map = {v: i for i, v in enumerate(results)}
    company_idx_map = {v: i for i, v in enumerate(companies_list)}

    def get_or_add(lst, idx_map, value):
        if value not in idx_map:
            idx_map[value] = len(lst)
            lst.append(value)
        return idx_map[value]

    print("fetching owners...")
    owners = fetch_owners()

    print("fetching MMC meetings since 2026-09-18...")
    meetings = fetch_mmc_meetings()
    print(f"  {len(meetings):,} meetings")

    m2c = batch_meeting_company_assoc([m["id"] for m in meetings])
    companies = batch_companies(m2c.values())

    now = datetime.now(timezone.utc)
    added, skipped_future = 0, 0
    for m in meetings:
        p = m["properties"]
        cid = m2c.get(m["id"])
        comp = companies.get(cid, {}) if cid else {}
        ts = p.get("hs_meeting_start_time")
        if not ts:
            continue
        d = parse_ts(ts)
        if d > now:
            skipped_future += 1
            continue
        date = d.date().isoformat()
        owner_id = p.get("hubspot_owner_id") or comp.get("hubspot_owner_id") or ""
        rep = normalize_name(owners.get(str(owner_id), "Unknown"))
        name = comp.get("name", "Unknown")
        addr = comp.get("address") or ""
        terr = comp.get("market2", "") or "(none)"

        company_i = get_or_add(companies_list, company_idx_map, name)
        if company_i == len(addresses):  # brand-new company -> extend parallel arrays
            z = ZIP_RE.findall(addr)
            addresses.append(addr)
            zips.append(z[-1] if z else "")

        row = [
            date,
            get_or_add(reps, rep_idx_map, rep),
            company_i,
            get_or_add(territories, terr_idx_map, terr),
            get_or_add(results, result_idx_map, "MMC Check-in"),
            1,  # source: MMC
            redact_contact_info((p.get("hs_meeting_body") or "").strip()),
        ]
        rows.append(row)
        added += 1

    rows.sort(key=lambda r: r[0])  # each row still ends with its note -- stays paired through the sort
    print(f"  added {added:,} MMC rows (skipped {skipped_future} future-dated)")

    notes_only = [r[6] for r in rows]
    rows_no_notes = [r[:6] for r in rows]

    out = {
        "reps": reps, "territories": territories, "results": results,
        "companies": companies_list, "addresses": addresses, "zips": zips,
        "sources": ["SPOTIO", "MMC"], "rows": rows_no_notes,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, separators=(",", ":")))
    OUT_NOTES_FILE.write_text(json.dumps(notes_only, separators=(",", ":")))
    print(f"wrote {OUT_FILE} ({OUT_FILE.stat().st_size / 1024 / 1024:.1f} MB, {len(rows):,} rows)")
    print(f"wrote {OUT_NOTES_FILE} ({OUT_NOTES_FILE.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
