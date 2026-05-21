#!/usr/bin/env python3
"""
Pull individual call records from Dialpad via the Stats API's records export
and count UNIQUE inbound callers (by external phone number) per contact center
per ISO week.

Approach:
  POST /api/v2/stats with stat_type='calls' AND export_type='records'
  → returns request_id
  → poll GET /api/v2/stats/{request_id} until status='complete'
  → download CSV from download_url
  → CSV has per-call rows with external_number + direction + date_started

Writes to a separate "WTG Dialpad Data" sheet:
  - unique_callers_raw            — center × week × unique caller count
  - unique_callers_by_market_week — pivot by WTG market

Env:
  DIALPAD_API_KEY
  GOOGLE_SA_JSON
  DIALPAD_SHEET_ID    (target sheet)
"""

import csv
import io
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials


REQUIRED = ["DIALPAD_API_KEY", "GOOGLE_SA_JSON"]
missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    print(f"ERROR: missing env vars: {missing}", file=sys.stderr)
    sys.exit(1)

SHEET_ID = os.environ.get("DIALPAD_SHEET_ID") or os.environ.get("GOOGLE_SHEET_ID")
if not SHEET_ID:
    print("ERROR: set DIALPAD_SHEET_ID or GOOGLE_SHEET_ID", file=sys.stderr)
    sys.exit(1)

API_KEY = os.environ["DIALPAD_API_KEY"]
BASE = "https://dialpad.com/api/v2"
H = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}

YEAR_START = date(date.today().year, 1, 1)
YEAR_END   = date.today()
DAYS_BACK  = (YEAR_END - YEAR_START).days


def _request_with_retry(method, url, **kwargs):
    kwargs.setdefault("timeout", 60)
    r = None
    for attempt in range(6):
        try:
            r = requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException:
            time.sleep(2 ** attempt); continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt); continue
        return r
    return r


def list_callcenters():
    centers = []
    cur = None
    while True:
        params = {"limit": 100}
        if cur: params["cursor"] = cur
        r = _request_with_retry("GET", f"{BASE}/callcenters", headers=H, params=params)
        body = r.json()
        centers.extend(body.get("items", []))
        cur = body.get("cursor")
        if not cur: break
    return centers


def request_records_export(target_id):
    """POST a records-export stats request. Returns request_id or None."""
    payload = {
        "days_ago_start": DAYS_BACK,
        "days_ago_end":   0,
        "target_id":      str(target_id),
        "target_type":    "callcenter",
        "stat_type":      "calls",
        "export_type":    "records",
        "is_today":       False,
        "timezone":       "America/Chicago",
        "coaching_group": False,
    }
    r = _request_with_retry("POST", f"{BASE}/stats",
                             headers={**H, "Content-Type": "application/json"},
                             json=payload)
    if r is None or r.status_code != 200:
        snippet = (r.text[:200] if r is not None else "") .replace("\n", " ")
        print(f"    ! stats POST {getattr(r,'status_code','?')}: {snippet}", flush=True)
        return None
    return r.json().get("request_id")


def poll_for_url(request_id, max_wait_s=900):  # 15 min — Dialpad records jobs sometimes take >5 min
    """Poll until status=='complete' and download_url is present, or timeout."""
    for _ in range(max_wait_s // 2):
        time.sleep(2)
        r = _request_with_retry("GET", f"{BASE}/stats/{request_id}", headers=H)
        if r is None or r.status_code != 200:
            continue
        body = r.json()
        status = body.get("status")
        if status == "complete" and body.get("download_url"):
            return body["download_url"]
        if status in ("failed", "error"):
            print(f"    ! stats job {request_id} failed: {body}", flush=True)
            return None
    print(f"    ! stats job {request_id} timed out", flush=True)
    return None


def download_csv(url):
    r = _request_with_retry("GET", url, headers=H, timeout=120)
    return r.text if r else ""


def normalize_number(n):
    """Strip non-digits, keep last 10 (US-centric phone normalization)."""
    if not n: return None
    digits = re.sub(r"\D", "", str(n))
    return digits[-10:] if len(digits) >= 10 else (digits or None)


def to_market(name):
    n = (name or "").lower()
    if "*dallas" in n:                       return "Dallas"
    if "*houston" in n:                      return "Houston"
    if "*san antonio" in n:                  return "San Antonio"
    if "*austin" in n:                       return "Austin"
    if "*phoenix" in n:                      return "Phoenix"
    if "*utah" in n:                         return "Utah"
    if "*tucson" in n:                       return "Tucson"
    if "scheduling office - spanish" in n:   return "Scheduling (Spanish)"
    return None


# ─────────────────────────────────────────────────────────────────────────────
print(f"Fetching contact centers…", flush=True)
centers = list_callcenters()
print(f"  → {len(centers)} centers", flush=True)
print(f"\nWindow: {YEAR_START} → {YEAR_END} ({DAYS_BACK} days)\n", flush=True)

# (center_name, iy, iw) → set of external numbers (normalized)
unique_callers = defaultdict(set)
total_inbound = 0
total_scanned = 0

for c in centers:
    cid = c.get("id")
    name = c.get("name") or f"Center {cid}"
    if not cid: continue

    rid = request_records_export(cid)
    if not rid:
        print(f"  ✗ {name}: stats request rejected", flush=True)
        continue
    url = poll_for_url(rid)
    if not url:
        print(f"  ✗ {name}: poll failed/timeout", flush=True)
        continue
    csv_text = download_csv(url)
    if not csv_text:
        print(f"  ✗ {name}: empty CSV", flush=True)
        continue

    # Records CSV has no "sep=" preamble; just plain CSV
    reader = csv.DictReader(io.StringIO(csv_text))
    n_scanned = n_inbound = 0
    for row in reader:
        n_scanned += 1
        if (row.get("direction") or "").lower() != "inbound":
            continue
        n_inbound += 1
        num = normalize_number(row.get("external_number"))
        if not num: continue
        ts = row.get("date_started") or ""
        if not ts: continue
        try:
            if ts.isdigit():
                dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
            else:
                # Handle e.g. "2026-04-21T13:55:12+00:00" or "2026-04-21 13:55:12"
                ts_clean = ts.replace("Z", "+00:00")
                try:
                    dt = datetime.fromisoformat(ts_clean)
                except ValueError:
                    dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        iy, iw, _ = dt.isocalendar()
        unique_callers[(name, iy, iw)].add(num)

    n_unique = sum(len(s) for k, s in unique_callers.items() if k[0] == name)
    print(f"  ✓ {name}: {n_scanned} scanned, {n_inbound} inbound, {n_unique} unique callers", flush=True)
    total_scanned += n_scanned
    total_inbound += n_inbound

print(f"\nTOTALS: {total_scanned} calls scanned, {total_inbound} inbound, "
      f"{len(unique_callers)} (center × week) buckets", flush=True)


# ─── Build per-center per-week rows ───
rows = []
for (name, iy, iw), nums in sorted(unique_callers.items()):
    rows.append({
        "contact_center": name,
        "iso_year":       iy,
        "iso_week":       iw,
        "unique_callers": len(nums),
    })


# ─── Write to sheet ───
print(f"\nConnecting to sheet {SHEET_ID[:12]}…", flush=True)
sa = Credentials.from_service_account_info(
    json.loads(os.environ["GOOGLE_SA_JSON"]),
    scopes=["https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"],
)
sh = gspread.authorize(sa).open_by_key(SHEET_ID)
print(f"  ✓ '{sh.title}'", flush=True)

# Raw tab
tab = "unique_callers_raw"
try:
    ws = sh.worksheet(tab); ws.clear()
except gspread.WorksheetNotFound:
    ws = sh.add_worksheet(title=tab, rows=max(len(rows) + 10, 100), cols=6)
if rows:
    headers = list(rows[0].keys())
    data = [headers] + [[r[h] for h in headers] for r in rows]
    ws.update(data, value_input_option="RAW")
    print(f"  ✓ wrote {len(rows)} rows to '{tab}'", flush=True)
else:
    ws.update([["contact_center", "iso_year", "iso_week", "unique_callers"],
               ["(no inbound calls found)", "", "", ""]], value_input_option="RAW")


# ─── Market pivot (dedupe at market level) ───
market_week_nums = defaultdict(set)
for (name, iy, iw), nums in unique_callers.items():
    m = to_market(name)
    if m:
        market_week_nums[(m, iw)].update(nums)

if market_week_nums:
    weeks = sorted({w for _, w in market_week_nums})
    ORDER = ["Dallas","Houston","Phoenix","Utah","San Antonio","Austin","Tucson","Scheduling (Spanish)"]
    markets = [m for m in ORDER if any((m, w) in market_week_nums for w in weeks)]
    header = ["Market"] + [f"W{w}" for w in weeks] + ["Total (unique across all weeks)"]
    out_rows = [header]
    col_tot = {w: 0 for w in weeks}
    all_nums = set()
    for m in markets:
        row = [m]
        market_all_nums = set()
        for w in weeks:
            n = len(market_week_nums.get((m, w), set()))
            row.append(n)
            col_tot[w] += n
            market_all_nums.update(market_week_nums.get((m, w), set()))
        row.append(len(market_all_nums))
        out_rows.append(row)
        all_nums.update(market_all_nums)
    out_rows.append(["TOTAL"] + [col_tot[w] for w in weeks] + [len(all_nums)])

    tab2 = "unique_callers_by_market_week"
    try:
        ws2 = sh.worksheet(tab2); ws2.clear()
    except gspread.WorksheetNotFound:
        ws2 = sh.add_worksheet(title=tab2, rows=len(out_rows) + 5, cols=len(out_rows[0]) + 1)
    ws2.update(out_rows, value_input_option="RAW")
    print(f"  ✓ wrote {len(out_rows) - 1} market rows to '{tab2}'", flush=True)

print(f"\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")
print("Done.")
