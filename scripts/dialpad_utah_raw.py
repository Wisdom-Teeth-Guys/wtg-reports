#!/usr/bin/env python3
"""Dump the FULL stats CSV for *Utah - WTG Main today (the active center, id 5803...)."""

import os, time, csv, io, requests

H = {"Authorization": f"Bearer {os.environ['DIALPAD_API_KEY']}", "Accept": "application/json"}
BASE = "https://dialpad.com/api/v2"

CID = "5803165713481728"  # the active *Utah - WTG Main today

payload = {
    "days_ago_start": 0, "days_ago_end": 0,
    "target_id": CID, "target_type": "callcenter",
    "stat_type": "calls", "export_type": "stats",
    "is_today": True, "timezone": "America/Chicago",
    "coaching_group": False,
}
r = requests.post(f"{BASE}/stats", headers={**H,"Content-Type":"application/json"}, json=payload, timeout=20)
rid = r.json()["request_id"]
print(f"request_id: {rid}", flush=True)

url = None
for _ in range(60):
    time.sleep(2)
    poll = requests.get(f"{BASE}/stats/{rid}", headers=H, timeout=20).json()
    if poll.get("status") == "complete" and poll.get("download_url"):
        url = poll["download_url"]; break
csv_text = requests.get(url, headers=H, timeout=30).text
print(f"\nCSV size: {len(csv_text)} chars, {csv_text.count(chr(10))} lines\n", flush=True)

reader = csv.DictReader(io.StringIO(csv_text))
rows = list(reader)
print(f"Total rows: {len(rows)}", flush=True)
if not rows:
    print("(empty)"); raise SystemExit
print(f"Columns: {len(rows[0])}\n", flush=True)

# Show every row, but only the columns that are interesting
interesting = ["hour","user_id","name","type","all_calls","inbound_calls","outbound_calls",
               "missed","abandoned","handled","answered","answered_transferred",
               "transferred_in","transferred_out","dtmf_transfer","auto_transfer",
               "router_transfer","forward_transfer","direct_to_voicemail",
               "in_queue_voicemail","transfer_voicemail","rejected","ring_no_answer"]
print("Per-row dump (non-zero values only):\n", flush=True)
for i, row in enumerate(rows):
    parts = []
    for k in interesting:
        if k not in row: continue
        v = row[k]
        if v and v not in ("0", "0.0", ""): parts.append(f"{k}={v}")
    if parts:
        print(f"  row {i}: " + ", ".join(parts), flush=True)
    else:
        # Show one summary line for "all-zero" rows
        if i < 3 or i == len(rows)-1:
            print(f"  row {i}: (all zeros — hour={row.get('hour')}, user_id={row.get('user_id', '-')})", flush=True)

# Sum totals
print("\nColumn totals:")
for k in interesting:
    if k not in rows[0]: continue
    try:
        s = sum(int(r[k] or 0) for r in rows)
        if s > 0: print(f"  {k} = {s}")
    except (ValueError, TypeError):
        pass
