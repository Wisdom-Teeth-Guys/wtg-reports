"""Generate a 26-week rotation schedule for one or all territories.

Rules:
  - VIP + Tier 1 cycle every 4 weeks    (each office visited ~6-7 times in 26w)
  - Tier 2 + Tier 3 cycle every 6 weeks (each office visited ~4-5 times in 26w)
  - Tier 4 (incl. untiered-but-has-wins, promoted) cycle every 12 weeks,
    but only consumes remaining weekly slots after mandatory tiers.
  - Zero (and untiered no-wins) fill any leftover slots,
    rotated most-stale-first.
  - Total slots per week is ORGS_PER_REP (50 by default).

Output:
  route_builder/output/schedule_<territory>_<start>/
    week_01.csv ... week_26.csv  — per-week stops
    summary.csv                  — long-form (one row per (week, hs_id))
    rotation_notes.txt           — counts + math summary

Usage:
  python3 -m route_builder.plan_route_schedule --territory "Dallas Northwest"
  python3 -m route_builder.plan_route_schedule --start 2026-06-22 --weeks 26
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import hubspot_client, territory_map
from .config import (
    HS_FIELDS,
    ORGS_PER_REP,
    OUTPUT_DIR,
    PROMOTE_TO_T4_MIN_WINS,
    SUPPRESSED_OUTCOMES,
)

# Cadence weeks per tier (None = no fixed cadence)
TIER_CYCLE_WEEKS = {
    "VIP":    4,
    "Tier 1": 4,
    "Tier 2": 6,
    "Tier 3": 6,
    "Tier 4": 12,
}


def parse_hs_date(s):
    if not s: return None
    try: return dt.date.fromisoformat(s[:10])
    except Exception: return None


def categorize_org(p: dict) -> str:
    """Return the effective tier bucket for an org, after T4-promotion."""
    raw = (p.get("tier_current") or "").strip()
    if raw in TIER_CYCLE_WEEKS:
        return raw
    wins = p.get("lifetime_won_deals")
    try: wins = int(float(wins)) if wins not in ("", None) else 0
    except: wins = 0
    if not raw and wins >= PROMOTE_TO_T4_MIN_WINS:
        return "Tier 4"
    if raw == "Tier 4":
        return "Tier 4"
    if raw == "Zero":
        return "Zero"
    # untiered with no wins
    return "Zero"


def fetch_territory_orgs(territory: str) -> list[dict]:
    """Pull every HS company in the named territory's zips."""
    zip_to_t = territory_map.load_zip_to_territory()
    target_zips = {z for z, t in zip_to_t.items() if t.lower() == territory.lower()}
    props = ["name", "address", "city", "state", "zip", "createdate",
             "lifetime_won_deals", *HS_FIELDS.values()]
    out = []
    for c in hubspot_client.iter_companies(props):
        z = (c.get("properties", {}).get("zip") or "").strip()[:5]
        if z in target_zips:
            out.append(c)
    return out


def assign_to_cycle(orgs: list[dict], cycle_weeks: int, n_weeks: int,
                   sort_key=lambda c: c["id"]) -> dict[int, list[dict]]:
    """Round-robin distribute orgs across `cycle_weeks` buckets, then repeat for
    n_weeks. Returns {week_number (1-indexed): [orgs]}.

    Bucketing by `id` keeps the rotation stable across runs (deterministic).
    """
    sorted_orgs = sorted(orgs, key=sort_key)
    result = defaultdict(list)
    for i, o in enumerate(sorted_orgs):
        bucket = i % cycle_weeks       # which slot in the cycle (0..cycle_weeks-1)
        for w in range(1, n_weeks + 1):
            if (w - 1) % cycle_weeks == bucket:
                result[w].append(o)
    return result


def build_schedule(territory: str, start_monday: dt.date, n_weeks: int,
                   slots_per_week: int) -> dict:
    """Return the full schedule structure for one territory."""
    print(f"Fetching {territory!r} companies from HubSpot…")
    all_orgs = fetch_territory_orgs(territory)
    # Filter out closed_permanent
    active = [c for c in all_orgs
              if (c.get("properties", {}).get(HS_FIELDS["last_outcome"]) or "") not in SUPPRESSED_OUTCOMES]
    # Categorize each
    by_bucket = defaultdict(list)
    for c in active:
        bucket = categorize_org(c.get("properties", {}))
        by_bucket[bucket].append(c)

    counts = {k: len(v) for k, v in by_bucket.items()}
    print(f"  buckets: {counts}")

    # Build per-week base rotation per tier
    vip_t1 = by_bucket["VIP"] + by_bucket["Tier 1"]
    t2_t3  = by_bucket["Tier 2"] + by_bucket["Tier 3"]
    t4     = by_bucket["Tier 4"]
    zero   = by_bucket["Zero"]

    vip_sched = assign_to_cycle(vip_t1, TIER_CYCLE_WEEKS["VIP"], n_weeks)
    t23_sched = assign_to_cycle(t2_t3,  TIER_CYCLE_WEEKS["Tier 2"], n_weeks)
    t4_sched  = assign_to_cycle(t4,     TIER_CYCLE_WEEKS["Tier 4"], n_weeks)

    # For each week, build the slate: mandatory first, then T4 fill, then Zero
    weekly: dict[int, list[tuple[str, dict]]] = {}  # week → [(reason, org_dict), ...]
    zero_cursor = 0  # roll-through pointer for Zero leftover
    for w in range(1, n_weeks + 1):
        slate: list[tuple[str, dict]] = []
        for o in vip_sched.get(w, []):
            tier = o.get("properties", {}).get("tier_current") or "VIP"
            slate.append((f"{tier} cadence (every 4w)", o))
        for o in t23_sched.get(w, []):
            tier = o.get("properties", {}).get("tier_current") or "Tier 3"
            slate.append((f"{tier} cadence (every 6w)", o))
        for o in t4_sched.get(w, []):
            if len(slate) >= slots_per_week: break
            slate.append(("Tier 4 priority fill (12w cycle)", o))
        # Top up from Zero
        while len(slate) < slots_per_week and zero_cursor < len(zero) * 5:
            o = zero[zero_cursor % len(zero)] if zero else None
            if not o: break
            slate.append(("Zero leftover fill", o))
            zero_cursor += 1
        # Cap at slots_per_week (mandatory may have overflowed)
        weekly[w] = slate[:slots_per_week]

    return {
        "territory": territory,
        "start_monday": start_monday.isoformat(),
        "n_weeks": n_weeks,
        "slots_per_week": slots_per_week,
        "counts": counts,
        "weekly": weekly,
    }


def write_schedule(sched: dict, out_dir: str) -> None:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    start = dt.date.fromisoformat(sched["start_monday"])
    n_weeks = sched["n_weeks"]
    rows_long = []
    for w in range(1, n_weeks + 1):
        week_monday = start + dt.timedelta(weeks=w - 1)
        slate = sched["weekly"].get(w, [])
        # Per-week CSV
        per_path = os.path.join(out_dir, f"week_{w:02d}_{week_monday}.csv")
        with open(per_path, "w", newline="") as f:
            ww = csv.writer(f)
            ww.writerow(["order", "hs_id", "name", "address", "city", "zip", "tier", "reason"])
            for i, (reason, o) in enumerate(slate, start=1):
                p = o.get("properties", {})
                ww.writerow([i, o["id"], p.get("name", ""), p.get("address", ""),
                             p.get("city", ""), p.get("zip", ""),
                             p.get("tier_current", ""), reason])
                rows_long.append((w, week_monday.isoformat(), i, reason, o, p))

    # Long-form summary CSV (all weeks)
    summ_path = os.path.join(out_dir, "summary.csv")
    with open(summ_path, "w", newline="") as f:
        ww = csv.writer(f)
        ww.writerow(["week_num", "week_starting", "order", "reason",
                     "hs_id", "name", "address", "city", "zip", "tier", "lifetime_won_deals"])
        for w, mon, order, reason, o, p in rows_long:
            ww.writerow([w, mon, order, reason, o["id"], p.get("name", ""),
                         p.get("address", ""), p.get("city", ""), p.get("zip", ""),
                         p.get("tier_current", ""), p.get("lifetime_won_deals", "")])

    # Notes / math
    notes = os.path.join(out_dir, "rotation_notes.txt")
    with open(notes, "w") as f:
        f.write(f"Schedule for: {sched['territory']}\n")
        f.write(f"Start Monday: {sched['start_monday']}\n")
        f.write(f"Horizon:      {n_weeks} weeks ({n_weeks * sched['slots_per_week']} total stops)\n")
        f.write(f"\nBucket counts:\n")
        for k, v in sorted(sched['counts'].items(), key=lambda x: -x[1]):
            f.write(f"  {k:10s} {v}\n")
        f.write(f"\nCadence rules:\n")
        for k, v in TIER_CYCLE_WEEKS.items():
            f.write(f"  {k:10s} every {v} weeks\n")
        f.write(f"  Zero       leftover fill only\n")

    print(f"Wrote: {out_dir}/")
    print(f"  - {n_weeks} per-week CSVs")
    print(f"  - summary.csv ({len(rows_long)} rows)")
    print(f"  - rotation_notes.txt")


def next_monday(reference: Optional[dt.date] = None) -> dt.date:
    d = reference or dt.date.today()
    return d - dt.timedelta(days=d.weekday())  # ISO Monday = 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--territory", default=None,
                    help="Territory name (matches territory_zip_map.csv). Default: all.")
    ap.add_argument("--start", default=None,
                    help="Start date (YYYY-MM-DD, must be a Monday). Default: this Monday.")
    ap.add_argument("--weeks", type=int, default=26)
    ap.add_argument("--slots", type=int, default=ORGS_PER_REP)
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start) if args.start else next_monday()
    if start.weekday() != 0:
        print(f"WARN: {start} is not a Monday; using {next_monday(start)} instead.")
        start = next_monday(start)

    zip_to_t = territory_map.load_zip_to_territory()
    territories = sorted(set(zip_to_t.values()))
    if args.territory:
        territories = [t for t in territories if t.lower() == args.territory.lower()]
        if not territories:
            print(f"ERROR: territory {args.territory!r} not found in territory_zip_map.csv")
            return 2

    for t in territories:
        sched = build_schedule(t, start, args.weeks, args.slots)
        safe = t.lower().replace(" ", "_")
        out_dir = os.path.join(OUTPUT_DIR, f"schedule_{safe}_{start}")
        write_schedule(sched, out_dir)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
