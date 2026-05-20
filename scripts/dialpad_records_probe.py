#!/usr/bin/env python3
"""Dedicated probe: pull export_type=records for Dallas Main and show CSV columns."""

import os, time, json, requests, csv, io

H = {"Authorization": f"Bearer {os.environ['DIALPAD_API_KEY']}", "Accept": "application/json"}
BASE = "https://dialpad.com/api/v2"

# Page through centers to find Dallas Main
centers = []
cur = None
while True:
    params = {"limit": 100}
    if cur: params["cursor"] = cur
    body = requests.get(f"{BASE}/callcenters", headers=H, params=params, timeout=20).json()
    centers.extend(body.get("items", []))
    cur = body.get("cursor")
    if not cur: break
target = next((c for c in centers if "*Dallas - WTG Main" in c.get("name", "")), centers[0])
print(f"Target: {target['name']} (id={target['id']})", flush=True)

payload = {
    "days_ago_start": 30, "days_ago_end": 0,
    "target_id": str(target["id"]), "target_type": "callcenter",
    "stat_type": "calls", "export_type": "records",
    "is_today": False, "timezone": "America/Chicago",
    "coaching_group": False,
}
print(f"POSTing /stats with export_type=records, 30 days back…", flush=True)
r = requests.post(f"{BASE}/stats", headers={**H, "Content-Type":"application/json"}, json=payload, timeout=30)
print(f"  HTTP {r.status_code}", flush=True)
print(f"  Response: {r.text[:300]}", flush=True)
if r.status_code != 200:
    raise SystemExit(1)
rid = r.json()["request_id"]
print(f"  Request ID: {rid}\n", flush=True)

print(f"Polling… (up to 5 min)", flush=True)
download_url = None
for i in range(150):
    time.sleep(2)
    poll = requests.get(f"{BASE}/stats/{rid}", headers=H, timeout=30).json()
    status = poll.get("status")
    if i % 5 == 0 or status == "complete":
        print(f"  poll {i+1}: status={status}  url={'YES' if poll.get('download_url') else 'NO'}", flush=True)
    if status == "complete" and poll.get("download_url"):
        download_url = poll["download_url"]
        break
    if status in ("failed", "error"):
        print(f"  failed: {poll}", flush=True)
        raise SystemExit(1)

if not download_url:
    print("Poll timed out", flush=True)
    raise SystemExit(1)

print(f"\nDownloading CSV from {download_url[:80]}…", flush=True)
csv_text = requests.get(download_url, headers=H, timeout=60).text
print(f"CSV size: {len(csv_text)} chars  ({csv_text.count(chr(10))} lines)", flush=True)

# Show first ~2KB
print(f"\n=== First 2500 chars ===")
print(csv_text[:2500])

# Parse headers
reader = csv.DictReader(io.StringIO(csv_text))
print(f"\n=== All column headers ===")
for c in (reader.fieldnames or []):
    print(f"  · {c}")

# Look for caller-related columns
print(f"\n=== Caller-related columns ===")
if reader.fieldnames:
    for c in reader.fieldnames:
        if any(k in c.lower() for k in ("caller","number","external","from","phone","contact")):
            print(f"  🎯 {c}")

# Print first 3 rows
print(f"\n=== First 3 data rows ===")
reader2 = csv.DictReader(io.StringIO(csv_text))
for i, row in enumerate(reader2):
    if i >= 3: break
    print(f"\n--- Row {i+1} ---")
    for k, v in row.items():
        if v: print(f"  {k}: {v}")
