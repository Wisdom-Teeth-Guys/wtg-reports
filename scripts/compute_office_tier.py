"""
Compute Company.tier_current from dental_referral_wins_last_90_days (quarterly).

TIER TRIGGER = WINS (won dental referrals) in the rolling last 90 days.

Thresholds (rolling 90-day dental WINS):
    VIP    >= 5      visit ~monthly
    Tier 1  3-4      ~every 6 weeks
    Tier 2  2        ~every 8-9 weeks
    Tier 3  1        ~quarterly
    Tier 4  0 wins in 90d AND 0 lifetime wins, but HAS referred
            → refers patients but we never convert (conversion problem)
    Zero    0 wins in 90d, but has won before (dormant winner → win-back)
    (blank) never referred at all

Placeholder / catch-all offices (names containing 'unknown', 'no office',
'no name') are EXCLUDED — their tier_current is cleared (blank).

Re-runnable; intended for a weekly cadence. Dry-run by default.

Usage:
    python3 compute_office_tier.py            # dry-run
    python3 compute_office_tier.py --push
"""
import argparse, json, os, time, urllib.request, urllib.error
from collections import Counter

BASE = os.path.dirname(__file__) or "."
# Token from env (GitHub Actions) first; fall back to local .env file.
HS = os.environ.get("HUBSPOT_TOKEN")
if not HS:
    _envpath = os.path.join(BASE, ".env")
    if os.path.exists(_envpath):
        env = {l.split("=",1)[0].strip(): l.split("=",1)[1].strip()
               for l in open(_envpath) if "=" in l and not l.startswith("#")}
        HS = env.get("HUBSPOT_TOKEN")
if not HS:
    raise SystemExit("HUBSPOT_TOKEN not found in environment or .env")
HDRS = {"Authorization": f"Bearer {HS}", "Content-Type": "application/json"}

PLACEHOLDER_PATTERNS = ("unknown", "no office", "no name", "no-office")
TIER_PROP = "dental_referral_wins_last_90_days"       # tier trigger = WINS in last 90d
LIFETIME_REFS_PROP = "dental_referrals_life_time"       # has it ever referred?
LIFETIME_WINS_PROP = "dental_referral_wins_all_time"    # has it ever won?


def tier_for(wins90: int, lifetime_refs: int, lifetime_wins: int) -> str:
    if wins90 >= 5: return "VIP"
    if wins90 >= 3: return "Tier 1"
    if wins90 == 2: return "Tier 2"
    if wins90 == 1: return "Tier 3"
    # 0 wins in last 90d:
    if lifetime_refs == 0: return ""           # never referred → blank
    if lifetime_wins == 0: return "Tier 4"     # referred but NEVER won (conversion problem)
    return "Zero"                              # won before, dormant → win-back


def is_placeholder(name: str) -> bool:
    n = (name or "").strip().lower()
    if not n:                      # blank / empty name → exclude
        return True
    return any(p in n for p in PLACEHOLDER_PATTERNS)


def get(url):
    for _ in range(5):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=HDRS)).read())
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(6); continue
            raise


def batch_update(updates):
    sent = 0
    for i in range(0, len(updates), 100):
        chunk = updates[i:i+100]
        for _ in range(5):
            try:
                urllib.request.urlopen(urllib.request.Request(
                    "https://api.hubapi.com/crm/v3/objects/companies/batch/update",
                    data=json.dumps({"inputs": chunk}).encode(), headers=HDRS, method="POST"))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429: time.sleep(6); continue
                raise
        sent += len(chunk)
        if i+100 < len(updates): time.sleep(0.1)
    return sent


def main(push):
    print(f"Computing tier_current from {TIER_PROP}  ({'PUSH' if push else 'DRY RUN'})\n")
    qs = (f"limit=100&properties=name&properties={TIER_PROP}"
          f"&properties={LIFETIME_REFS_PROP}&properties={LIFETIME_WINS_PROP}"
          f"&properties=tier_current")
    after = None
    updates = []
    dist = Counter()
    excluded = 0
    scanned = 0
    while True:
        url = f"https://api.hubapi.com/crm/v3/objects/companies?{qs}"
        if after: url += f"&after={after}"
        d = get(url)
        for c in d.get("results", []):
            scanned += 1
            p = c.get("properties") or {}
            name = p.get("name") or ""
            cur = p.get("tier_current") or ""
            def _iv(key):
                v = p.get(key)
                return int(float(v)) if v not in (None, "") else 0
            wins = _iv(TIER_PROP)
            lifetime_refs = _iv(LIFETIME_REFS_PROP)
            lifetime_wins = _iv(LIFETIME_WINS_PROP)

            if is_placeholder(name):
                excluded += 1
                desired = ""   # clear — excluded from tiering
            else:
                desired = tier_for(wins, lifetime_refs, lifetime_wins)

            dist[desired or "(excluded)"] += 1
            if (cur or "") != desired:
                updates.append({"id": c["id"], "properties": {"tier_current": desired}})
        after = (d.get("paging") or {}).get("next", {}).get("after")
        if not after: break
        time.sleep(0.05)

    print(f"Scanned {scanned:,} companies. Excluded {excluded} placeholders.\n")
    print("Target tier distribution:")
    for k in ("VIP","Tier 1","Tier 2","Tier 3","Tier 4","Zero","(excluded)"):
        print(f"  {k:<12} {dist.get(k,0):>7,}")
    print(f"\n{len(updates):,} companies need tier_current changed.")

    if not push:
        print("\nDRY RUN — re-run with --push to write.")
        return
    print(f"\nWriting {len(updates):,} updates...")
    sent = batch_update(updates)
    print(f"  updated {sent:,}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    main(ap.parse_args().push)
