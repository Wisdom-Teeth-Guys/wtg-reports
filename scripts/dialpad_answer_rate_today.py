#!/usr/bin/env python3
"""One-shot: print today's answer rate across all inbound *Main contact centers."""

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

# Only main inbound centers (the * prefixed ones + Scheduling Spanish)
def is_main(name):
    n = (name or "").lower()
    return any(k in n for k in ['*dallas','*houston','*san antonio','*austin','*phoenix','*utah','*tucson']) \
        or 'scheduling office - spanish' in n

mains = [c for c in centers if is_main(c.get("name"))]
print(f"Inbound centers being queried: {len(mains)}\n", flush=True)

def to_market(n):
    n = (n or "").lower()
    if '*dallas' in n: return 'Dallas'
    if '*houston' in n: return 'Houston'
    if '*san antonio' in n: return 'San Antonio'
    if '*austin' in n: return 'Austin'
    if '*phoenix' in n: return 'Phoenix'
    if '*utah' in n: return 'Utah'
    if '*tucson' in n: return 'Tucson'
    if 'scheduling office - spanish' in n: return 'Scheduling (Spanish)'
    return 'Other'

market_total = {}     # market -> total inbound
market_answered = {}  # market -> answered

for c in mains:
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
        print(f"  ✗ {name}: POST {r.status_code} {r.text[:120]}", flush=True); continue
    rid = r.json()["request_id"]
    url = None
    for _ in range(60):
        time.sleep(2)
        poll = requests.get(f"{BASE}/stats/{rid}", headers=H, timeout=20).json()
        if poll.get("status") == "complete" and poll.get("download_url"):
            url = poll["download_url"]; break
        if poll.get("status") in ("failed", "error"):
            print(f"  ✗ {name}: poll {poll}", flush=True); break
    if not url: continue
    csv_text = requests.get(url, headers=H, timeout=30).text
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    # Find inbound and answered columns
    if not rows: continue
    headers = list(rows[0].keys())
    # Try common header names
    total_col = next((h for h in headers if h.lower() in ('inbound_calls','total_inbound','inbound')), None)
    ans_col = next((h for h in headers if 'answered' in h.lower() and 'inbound' in h.lower()), None) \
            or next((h for h in headers if h.lower() in ('answered','total_answered')), None)
    if not total_col or not ans_col:
        print(f"  ! {name}: columns unclear. Headers: {headers}", flush=True); continue
    tot = sum(int(r[total_col] or 0) for r in rows)
    ans = sum(int(r[ans_col] or 0) for r in rows)
    m = to_market(name)
    market_total[m]    = market_total.get(m, 0) + tot
    market_answered[m] = market_answered.get(m, 0) + ans
    print(f"  ✓ {name}: {ans}/{tot}", flush=True)

# Summary
print("\n" + "="*60)
print(f"{'Market':<22} {'Answered':>10} {'Total':>8} {'Rate':>8}")
print("="*60)
grand_t, grand_a = 0, 0
for m in ['Dallas','Houston','Phoenix','Utah','San Antonio','Austin','Tucson','Scheduling (Spanish)']:
    t = market_total.get(m, 0); a = market_answered.get(m, 0)
    if t == 0: continue
    rate = a / t * 100 if t else 0
    print(f"{m:<22} {a:>10} {t:>8} {rate:>7.1f}%")
    grand_t += t; grand_a += a
print("-"*60)
rate = grand_a / grand_t * 100 if grand_t else 0
print(f"{'TODAY TOTAL':<22} {grand_a:>10} {grand_t:>8} {rate:>7.1f}%")
