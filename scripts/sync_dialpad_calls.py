#!/usr/bin/env python3
"""
Sync Dialpad inbound calls by contact center by week.

Pulls inbound call counts for each Dialpad call center (a.k.a. contact center)
for 2026 YTD and writes weekly aggregates to a `dialpad_calls_raw` tab.

Env vars:
  DIALPAD_API_KEY
  GOOGLE_SHEET_ID
  GOOGLE_SA_JSON
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials


REQUIRED = ["DIALPAD_API_KEY", "GOOGLE_SHEET_ID", "GOOGLE_SA_JSON"]
missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    print(f"ERROR: missing env vars: {missing}", file=sys.stderr)
    sys.exit(1)

API_KEY = os.environ["DIALPAD_API_KEY"]
BASE = "https://dialpad.com/api/v2"
H = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}

YEAR_START = date(date.today().year, 1, 1)
YEAR_END   = date.today()


def get_paginated(path: str, params: dict = None):
    """Walk Dialpad cursor pagination."""
    params = dict(params or {})
    out = []
    while True:
        r = requests.get(f"{BASE}{path}", headers=H, params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        items = body.get("items") or body.get("data") or []
        out.extend(items)
        cursor = body.get("cursor")
        if not cursor:
            break
        params["cursor"] = cursor
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 1) List contact centers (callcenters)
# ─────────────────────────────────────────────────────────────────────────────
print("Fetching Dialpad contact centers…", flush=True)
centers = get_paginated("/callcenters")
print(f"  → {len(centers)} contact centers")

if not centers:
    print("No contact centers found — exiting without writing", file=sys.stderr)
    sys.exit(0)

for c in centers[:5]:
    print(f"  · {c.get('name')} (id={c.get('id')})")


# ─────────────────────────────────────────────────────────────────────────────
# 2) For each center, kick off a stats request and poll for completion
# ─────────────────────────────────────────────────────────────────────────────
def request_stats(target_id: str, target_type: str = "callcenter"):
    """Submit an async stats request and return the request_id."""
    payload = {
        "days_ago_start":   (YEAR_END - YEAR_START).days,
        "days_ago_end":     0,
        "target_id":        target_id,
        "target_type":      target_type,
        "stat_type":        "calls",
        "export_type":      "stats",
        "is_today":         False,
        "timezone":         "America/Chicago",
        "coaching_group":   False,
        # group_by removed — Dialpad rejected "day" as invalid enum.
        # The stats CSV is daily by default for "calls" stat_type.
    }
    r = requests.post(f"{BASE}/stats", headers={**H, "Content-Type": "application/json"},
                      json=payload, timeout=30)
    if r.status_code >= 400:
        print(f"    stats request failed: {r.status_code} {r.text[:300]}", flush=True)
        return None
    return r.json().get("request_id")


def poll_stats(request_id: str, max_wait_s: int = 120):
    """Poll until the stats request is ready, return file URL or rows."""
    for _ in range(max_wait_s // 2):
        r = requests.get(f"{BASE}/stats/{request_id}", headers=H, timeout=30)
        if r.status_code == 200:
            body = r.json()
            if body.get("status") in ("complete", "completed", "done"):
                return body
            if body.get("status") == "failed":
                print(f"    stats failed: {body}", flush=True)
                return None
        time.sleep(2)
    print(f"    stats poll timed out for {request_id}", flush=True)
    return None


def fetch_stats_file(file_url: str):
    """Download the stats CSV (Dialpad returns CSV files)."""
    r = requests.get(file_url, headers=H, timeout=60)
    r.raise_for_status()
    return r.text


# Collect inbound call counts by (center_name, iso_year, iso_week)
import csv
import io
from collections import Counter

weekly_rows = []  # one row per (center, year, week)
print("\nRequesting stats per center…", flush=True)
for c in centers:
    name = c.get("name") or f"Center {c.get('id')}"
    cid = c.get("id")
    if not cid:
        continue
    print(f"  → {name} (id={cid})", flush=True)
    rid = request_stats(str(cid))
    if not rid:
        continue
    body = poll_stats(rid)
    if not body:
        continue
    file_url = body.get("file_url") or body.get("download_url")
    if not file_url:
        print(f"    no file_url in response: {body}", flush=True)
        continue
    csv_text = fetch_stats_file(file_url)
    # Parse CSV — expected columns include date + call counts (inbound/outbound)
    reader = csv.DictReader(io.StringIO(csv_text))
    weekly = Counter()  # (year, week) → inbound calls
    for row in reader:
        date_str = row.get("date") or row.get("Date") or row.get("day")
        if not date_str:
            continue
        try:
            d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        # Look for an inbound calls column
        inbound = None
        for k in ("inbound_calls", "calls_inbound", "inbound", "Inbound", "Inbound Calls"):
            if k in row:
                inbound = row[k]; break
        if inbound is None:
            # No inbound-specific column — total calls fallback
            for k in ("total_calls", "calls", "call_count", "Calls"):
                if k in row: inbound = row[k]; break
        try:
            n = int(float(inbound or 0))
        except ValueError:
            n = 0
        iy, iw, _ = d.isocalendar()
        weekly[(iy, iw)] += n

    for (iy, iw), n in sorted(weekly.items()):
        weekly_rows.append({
            "contact_center":  name,
            "iso_year":        iy,
            "iso_week":        iw,
            "inbound_calls":   n,
        })

print(f"\n  → {len(weekly_rows)} (center × week) rows total", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# 3) Write to Google Sheet
# ─────────────────────────────────────────────────────────────────────────────
print("\nWriting to Google Sheet…", flush=True)
sa = Credentials.from_service_account_info(
    json.loads(os.environ["GOOGLE_SA_JSON"]),
    scopes=["https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"],
)
sh = gspread.authorize(sa).open_by_key(os.environ["GOOGLE_SHEET_ID"])

tab = "dialpad_calls_raw"
try:
    ws = sh.worksheet(tab); ws.clear()
except gspread.WorksheetNotFound:
    ws = sh.add_worksheet(title=tab, rows=max(len(weekly_rows) + 10, 100), cols=8)

if weekly_rows:
    headers = list(weekly_rows[0].keys())
    data = [headers] + [[r.get(h, "") for h in headers] for r in weekly_rows]
    ws.update(data, value_input_option="RAW")
    print(f"  ✓ wrote {len(weekly_rows)} rows", flush=True)
else:
    ws.update([["contact_center", "iso_year", "iso_week", "inbound_calls"],
               ["(no data returned)", "", "", ""]], value_input_option="RAW")

# meta
try:
    meta = sh.worksheet("_meta")
    found = False
    for i, row in enumerate(meta.get_all_records()):
        if row.get("key") == "dialpad_last_synced_utc":
            meta.update_cell(i + 2, 2, datetime.now(timezone.utc).isoformat(timespec="seconds"))
            found = True; break
    if not found:
        meta.append_row(["dialpad_last_synced_utc",
                         datetime.now(timezone.utc).isoformat(timespec="seconds")])
except Exception as e:
    print(f"  (couldn't update _meta: {e})", flush=True)

print("Done.")
