#!/usr/bin/env python3
"""Diagnostic: show ALL Utah-related contact centers and today's calls per each."""

import os, time, csv, io, requests

H = {"Authorization": f"Bearer {os.environ['DIALPAD_API_KEY']}", "Accept": "application/json"}
BASE = "https://dialpad.com/api/v2"

# Get all centers
centers = []
cur = None
while True:
    params = {"limit": 100}
    if cur: params["cursor"] = cur
    body = requests.get(f"{BASE}/callcenters", headers=H, params=params, timeout=20).json()
    centers.extend(body.get("items", []))
    cur = body.get("cursor")
    if not cur: break

# All Utah-related centers
utah_centers = [c for c in centers if "utah" in (c.get("name") or "").lower()]
print(f"All Utah-related centers ({len(utah_centers)}):\n", flush=True)

for c in utah_centers:
    cid, name = str(c["id"]), c.get("name", "?")
    payload = {
        "days_ago_start": 0, "days_ago_end": 0,
        "target_id": cid, "target_type": "callcenter",
        "stat_type": "calls", "export_type": "stats",
        "is_today": True, "timezone": "America/Chicago",
        "coaching_group": False,
    }
    r = requests.post(f"{BASE}/stats", headers={**H,"Content-Type":"application/json"}, json=payload, timeout=20)
    if r.status_code != 200:
        print(f"  {name} (id={cid}): POST {r.status_code}", flush=True); continue
    rid = r.json()["request_id"]
    url = None
    for _ in range(60):
        time.sleep(2)
        poll = requests.get(f"{BASE}/stats/{rid}", headers=H, timeout=20).json()
        if poll.get("status") == "complete" and poll.get("download_url"):
            url = poll["download_url"]; break
    if not url:
        print(f"  {name} (id={cid}): poll timeout", flush=True); continue
    csv_text = requests.get(url, headers=H, timeout=30).text
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        print(f"  {name:<40} (id={cid}): 0 rows", flush=True); continue
    headers = list(rows[0].keys())
    # Show ALL columns first time
    if c == utah_centers[0]:
        print(f"  [column headers]: {', '.join(headers)}\n", flush=True)
    # Sum every numeric column
    sums = {}
    for h in headers:
        try:
            sums[h] = sum(int(r[h] or 0) for r in rows)
        except (ValueError, TypeError):
            pass
    nonzero = {k:v for k,v in sums.items() if v > 0}
    summary = ", ".join(f"{k}={v}" for k,v in sorted(nonzero.items(), key=lambda x:-x[1]))
    print(f"  {name:<40} (id={cid}): {summary or '(all zeros)'}", flush=True)
