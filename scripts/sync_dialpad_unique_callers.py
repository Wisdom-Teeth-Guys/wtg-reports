#!/usr/bin/env python3
"""
Pull individual call records from Dialpad and count UNIQUE inbound callers
(by external phone number) per contact center per ISO week.

Writes to a separate "WTG Dialpad Data" sheet:
  - unique_callers_raw           — center × week × unique caller count
  - unique_callers_by_market_week — pivot by market

Env:
  DIALPAD_API_KEY
  GOOGLE_SA_JSON
  DIALPAD_SHEET_ID   (target sheet)
"""

import json
import os
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

YEAR_START_MS = int(datetime(date.today().year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
NOW_MS = int(datetime.now(timezone.utc).timestamp() * 1000)


def _request_with_retry(method, url, **kwargs):
    """HTTP request with retry on 429/5xx."""
    for attempt in range(6):
        try:
            r = requests.request(method, url, timeout=60, **kwargs)
        except requests.exceptions.RequestException as e:
            time.sleep(2 ** attempt); continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt); continue
        return r
    return r


# ─── 1) List call centers ───
print("Fetching contact centers…", flush=True)
centers = []
cursor = None
while True:
    params = {"limit": 100}
    if cursor: params["cursor"] = cursor
    r = _request_with_retry("GET", f"{BASE}/callcenters", headers=H, params=params)
    body = r.json()
    centers.extend(body.get("items", []))
    cursor = body.get("cursor")
    if not cursor: break
print(f"  → {len(centers)} centers", flush=True)


# ─── 2) For each center, paginate /calls and collect unique caller numbers per week ───
def normalize_number(n):
    """Strip non-digits, keep last 10 digits (US-centric)."""
    if not n: return None
    digits = "".join(c for c in str(n) if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else (digits or None)


# (center_name, iso_year, iso_week) -> set of external numbers
unique_callers = defaultdict(set)
total_calls_seen = 0

for c in centers:
    cid = c.get("id")
    name = c.get("name") or f"Center {cid}"
    if not cid: continue
    print(f"  → {name}…", flush=True, end=" ")
    n_calls = 0
    cursor = None
    while True:
        params = {
            "target_id":      cid,
            "target_type":    "callcenter",
            "started_after":  YEAR_START_MS,
            "started_before": NOW_MS,
            "limit": 200,
        }
        if cursor: params["cursor"] = cursor
        r = _request_with_retry("GET", f"{BASE}/calls", headers=H, params=params)
        if r.status_code != 200:
            print(f"\n    ! /calls {r.status_code}: {r.text[:200]}", flush=True)
            break
        body = r.json()
        items = body.get("items", [])
        for call in items:
            n_calls += 1
            # Inbound only — direction may be "inbound", "outbound", "internal"
            direction = (call.get("direction") or "").lower()
            if direction != "inbound":
                continue
            # External (customer-side) phone number
            ext = call.get("external_number") or call.get("from") or call.get("caller_number")
            num = normalize_number(ext)
            if not num: continue
            # Timestamp -> ISO week
            ts = call.get("date_started") or call.get("started_at")
            if not ts: continue
            try:
                if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
                    dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
                else:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                continue
            iy, iw, _ = dt.isocalendar()
            unique_callers[(name, iy, iw)].add(num)
        cursor = body.get("cursor")
        if not cursor: break
    total_calls_seen += n_calls
    n_unique = sum(len(s) for k, s in unique_callers.items() if k[0] == name)
    print(f"{n_calls} calls, {n_unique} unique inbound callers", flush=True)

print(f"\nTotal calls scanned: {total_calls_seen}", flush=True)
print(f"Total (center × week) buckets: {len(unique_callers)}", flush=True)


# ─── 3) Build rows ───
rows = []
for (name, iy, iw), nums in sorted(unique_callers.items()):
    rows.append({
        "contact_center":  name,
        "iso_year":        iy,
        "iso_week":        iw,
        "unique_callers":  len(nums),
    })


# ─── 4) Write to sheet ───
sa = Credentials.from_service_account_info(
    json.loads(os.environ["GOOGLE_SA_JSON"]),
    scopes=["https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"],
)
sh = gspread.authorize(sa).open_by_key(SHEET_ID)
print(f"\nConnected to sheet: '{sh.title}'", flush=True)

# Raw tab
tab = "unique_callers_raw"
try:
    ws = sh.worksheet(tab); ws.clear()
except gspread.WorksheetNotFound:
    ws = sh.add_worksheet(title=tab, rows=max(len(rows) + 10, 100), cols=6)
if rows:
    data = [list(rows[0].keys())] + [[r.get(h, "") for h in rows[0].keys()] for r in rows]
    ws.update(data, value_input_option="RAW")
    print(f"  ✓ wrote {len(rows)} rows to '{tab}'", flush=True)
else:
    ws.update([["contact_center","iso_year","iso_week","unique_callers"],
               ["(no inbound calls returned)","","",""]], value_input_option="RAW")


# ─── 5) Market pivot ───
def to_market(n):
    n = n.lower()
    if '*dallas' in n: return 'Dallas'
    if '*houston' in n: return 'Houston'
    if '*san antonio' in n: return 'San Antonio'
    if '*austin' in n: return 'Austin'
    if '*phoenix' in n: return 'Phoenix'
    if '*utah' in n: return 'Utah'
    if '*tucson' in n: return 'Tucson'
    if 'scheduling office - spanish' in n: return 'Scheduling (Spanish)'
    return None

# Important: dedupe at the MARKET level (a caller calling Dallas A AND Dallas B = 1 unique)
market_week_nums = defaultdict(set)
for (name, iy, iw), nums in unique_callers.items():
    m = to_market(name)
    if m:
        market_week_nums[(m, iw)].update(nums)

if market_week_nums:
    weeks = sorted({w for _, w in market_week_nums})
    ORDER = ['Dallas','Houston','Phoenix','Utah','San Antonio','Austin','Tucson','Scheduling (Spanish)']
    markets = [m for m in ORDER if any((m, w) in market_week_nums for w in weeks)]
    header = ["Market"] + [f"W{w}" for w in weeks] + ["Total"]
    out_rows = [header]
    col_tot = {w: 0 for w in weeks}
    for m in markets:
        row = [m]; mtotal = 0
        # For "Total" column, dedupe across ALL weeks for this market
        all_nums_for_market = set()
        for w in weeks:
            n = len(market_week_nums.get((m, w), set()))
            row.append(n)
            col_tot[w] += n
            all_nums_for_market.update(market_week_nums.get((m, w), set()))
        row.append(len(all_nums_for_market))  # true unique callers across all weeks
        out_rows.append(row)
    grand_total_nums = set()
    for nums in market_week_nums.values(): grand_total_nums.update(nums)
    out_rows.append(["TOTAL"] + [col_tot[w] for w in weeks] + [len(grand_total_nums)])

    tab2 = "unique_callers_by_market_week"
    try:
        ws2 = sh.worksheet(tab2); ws2.clear()
    except gspread.WorksheetNotFound:
        ws2 = sh.add_worksheet(title=tab2, rows=len(out_rows)+5, cols=len(out_rows[0])+1)
    ws2.update(out_rows, value_input_option="RAW")
    print(f"  ✓ wrote market pivot ({len(out_rows)-1} rows) to '{tab2}'", flush=True)

print(f"\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")
print("Done.")
