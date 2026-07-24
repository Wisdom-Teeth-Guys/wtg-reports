"""
Planned-vs-actual route audit.

For a given week (Monday → Thursday), find every HubSpot company that was
stamped with `visit_week_of = <Monday>`, then check whether SPOTIO recorded
any field visit at that org during that week.

The KEY metric: **contact rate** (planned orgs where the rep actually had a
conversation, not just a closed-door drive-by). Brooklyn's 2,160 visits had a
47% closed-door rate — visit count alone doesn't show that; contact rate does.

Usage:
    python3 -m route_builder.route_audit_report --week-of 2026-05-04
    python3 -m route_builder.route_audit_report --week-of 2026-05-04 --apply-boost --push

Output:
    route_builder/output/audit_YYYY-MM-DD/
        audit.csv         — per-org rows: planned, visited, outcome, contacted (bool)
        per_territory.csv — per-territory rollup with visit% + contact%
        summary.txt       — top-line numbers
"""
import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import address_matcher, hubspot_client, spotio_client
from .config import (
    CLOSED_OUTCOMES, CONTACTED_OUTCOMES, HS_EXISTING_FIELDS, HS_FIELDS,
    NON_VISIT_RESULT_IDS, OUTPUT_DIR, SPOTIO_RESULT_TO_OUTCOME,
    SPOTIO_VISIT_TEMPLATE_ID,
)


def _parse_iso(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, _, tail = s.partition(".")
        m = ""
        for ch in tail:
            if ch.isdigit() and len(m) < 6:
                m += ch
            elif ch.isdigit():
                continue
            else:
                tail = m + tail[tail.index(ch):]
                break
        else:
            tail = m
        s = head + "." + tail
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def fetch_planned_orgs(week_of: date) -> list[dict]:
    """All HubSpot companies stamped with visit_week_of = the given Monday."""
    # HubSpot's search API wants epoch milliseconds for date fields
    week_dt = datetime.combine(week_of, datetime.min.time(), tzinfo=timezone.utc)
    epoch_ms = int(week_dt.timestamp() * 1000)
    return hubspot_client.search_companies(
        filter_groups=[{
            "filters": [{
                "propertyName": HS_FIELDS["week_of"],
                "operator": "EQ",
                "value": epoch_ms,
            }]
        }],
        properties=[
            "name", "address", "city", "state", "zip",
            HS_EXISTING_FIELDS["territory"],
            HS_EXISTING_FIELDS["marketer_assigned"],
            *HS_FIELDS.values(),
        ],
    )


def fetch_spotio_visits_for_week(week_start: date) -> dict:
    """Build {address_signature: [visit_records]} for visits in [week_start, week_start+5).

    Walks SPOTIO leads with recent activity, fetches activities for each, filters
    to visits during the audit window. Returns visits indexed by lead's address signature.
    """
    window_start = datetime.combine(week_start, datetime.min.time(), tzinfo=timezone.utc)
    window_end = window_start + timedelta(days=5)  # Mon-Fri inclusive
    print(f"  Window: {window_start.date()} .. {window_end.date()}")
    visits_by_sig = defaultdict(list)
    leads_scanned = 0
    visits_kept = 0
    for lead in spotio_client.iter_leads(page_size=100):
        leads_scanned += 1
        if leads_scanned % 1000 == 0:
            print(f"    scanned {leads_scanned:,} leads, kept {visits_kept} visits...")
        la = _parse_iso(lead.get("lastActivityTime", ""))
        if not la or la < window_start - timedelta(days=2):
            # Sorted desc by lastActivityTime → safe to break once we're well before window
            if la and la < window_start - timedelta(days=14):
                break
            continue
        try:
            acts = spotio_client.fetch_lead_activities(lead["id"])
        except Exception:
            continue
        pin = lead.get("pin") or {}
        sig = address_matcher.address_signature(pin.get("address"), pin.get("zip"))
        if not sig:
            continue
        for a in acts:
            if (a.get("type") != "event" or
                    a.get("activityTemplateId") != SPOTIO_VISIT_TEMPLATE_ID or
                    not a.get("done")):
                continue
            d = _parse_iso(a.get("date", ""))
            if not d or not (window_start <= d < window_end):
                continue
            rid = str(a.get("resultId") or "")
            if rid in NON_VISIT_RESULT_IDS:
                continue
            visits_by_sig[sig].append({
                "date": d,
                "resultId": rid,
                "outcome": SPOTIO_RESULT_TO_OUTCOME.get(rid),
                "ownerId": a.get("ownerId"),
                "notes": a.get("notes", ""),
            })
            visits_kept += 1
    print(f"  Done: scanned {leads_scanned:,} leads, kept {visits_kept} visits in window")
    return dict(visits_by_sig)


def main():
    p = argparse.ArgumentParser(description="Planned-vs-actual route audit")
    p.add_argument("--week-of", required=True, help="The Monday of the week to audit (YYYY-MM-DD)")
    p.add_argument("--apply-boost", action="store_true",
                   help="Set falloff_flag=True on orgs that were planned but had no contact")
    p.add_argument("--push", action="store_true",
                   help="Required alongside --apply-boost to actually write to HubSpot")
    p.add_argument("--output-dir", help="Override output directory")
    args = p.parse_args()

    week_of = date.fromisoformat(args.week_of)
    today = date.today()
    out_dir = args.output_dir or os.path.join(OUTPUT_DIR, f"audit_{today.isoformat()}")
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    print(f"\n=== Route Audit — week of {week_of} ===\n")

    # 1. Fetch planned orgs
    print("[1/3] Fetching planned orgs from HubSpot...")
    t0 = time.time()
    planned = fetch_planned_orgs(week_of)
    print(f"  {len(planned)} orgs were stamped with visit_week_of={week_of} ({time.time()-t0:.1f}s)\n")
    if not planned:
        print("Nothing to audit — no orgs were stamped for that week.")
        return 0

    # 2. Pull SPOTIO visits that occurred in the window
    print("[2/3] Pulling SPOTIO visits in the audit window...")
    visits_by_sig = fetch_spotio_visits_for_week(week_of)
    print()

    # 3. Cross-reference
    print("[3/3] Cross-referencing...")
    rows = []
    per_territory = defaultdict(lambda: {"planned": 0, "visited": 0, "contacted": 0,
                                           "closed": 0, "no_outcome": 0})
    for hs in planned:
        p = hs.get("properties") or {}
        sig = address_matcher.address_signature(p.get("address"), p.get("zip"))
        territory = p.get(HS_EXISTING_FIELDS["territory"]) or "(unknown)"
        visits = visits_by_sig.get(sig, []) if sig else []
        visited = bool(visits)
        outcomes = [v["outcome"] for v in visits if v["outcome"]]
        contacted = any(o in CONTACTED_OUTCOMES for o in outcomes)
        closed = any(o in CLOSED_OUTCOMES for o in outcomes)
        per_territory[territory]["planned"] += 1
        if visited:
            per_territory[territory]["visited"] += 1
        if contacted:
            per_territory[territory]["contacted"] += 1
        elif closed:
            per_territory[territory]["closed"] += 1
        elif visited:
            per_territory[territory]["no_outcome"] += 1
        rows.append({
            "hs_id":       hs["id"],
            "name":        p.get("name", ""),
            "territory":   territory,
            "address":     p.get("address", ""),
            "zip":         p.get("zip", ""),
            "score":       p.get(HS_FIELDS["priority_score"], ""),
            "visit_reason": p.get(HS_FIELDS["visit_reason"], ""),
            "planned":     True,
            "visited":     visited,
            "visits_count": len(visits),
            "outcomes":    "|".join([o for o in outcomes if o]),
            "contacted":   contacted,
            "closed":      closed and not contacted,
        })

    # Write CSVs
    audit_csv = os.path.join(out_dir, "audit.csv")
    with open(audit_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  audit.csv ({len(rows)} rows): {audit_csv}")

    territory_csv = os.path.join(out_dir, "per_territory.csv")
    with open(territory_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["territory", "planned", "visited", "visit_pct",
                    "contacted", "contact_pct", "closed_doors", "no_outcome"])
        for t, s in sorted(per_territory.items()):
            visit_pct = s["visited"] / s["planned"] * 100 if s["planned"] else 0
            contact_pct = s["contacted"] / s["planned"] * 100 if s["planned"] else 0
            w.writerow([t, s["planned"], s["visited"], f"{visit_pct:.1f}%",
                        s["contacted"], f"{contact_pct:.1f}%", s["closed"], s["no_outcome"]])
    print(f"  per_territory.csv: {territory_csv}")

    # Top-line summary
    total = len(rows)
    visited = sum(1 for r in rows if r["visited"])
    contacted = sum(1 for r in rows if r["contacted"])
    closed = sum(1 for r in rows if r["closed"])
    no_outcome = sum(1 for r in rows if r["visited"] and not r["outcomes"])

    summary_lines = [
        f"Route Audit — Week of {week_of}",
        f"",
        f"  Planned:        {total}",
        f"  Visited:        {visited}  ({visited/total*100:.1f}% visit compliance)",
        f"  Contacted:      {contacted}  ({contacted/total*100:.1f}% contact rate)  ← the number that matters",
        f"  Closed doors:   {closed}",
        f"  No outcome:     {no_outcome}  (check-in logged but outcome blank)",
        f"",
        f"Per-territory breakdown:",
    ]
    for t, s in sorted(per_territory.items()):
        visit_pct = s["visited"] / s["planned"] * 100 if s["planned"] else 0
        contact_pct = s["contacted"] / s["planned"] * 100 if s["planned"] else 0
        summary_lines.append(
            f"  {t:<25}  planned={s['planned']:>3}  "
            f"visit={visit_pct:>5.1f}%  contact={contact_pct:>5.1f}%  "
            f"closed-doors={s['closed']}"
        )

    text = "\n".join(summary_lines)
    with open(os.path.join(out_dir, "summary.txt"), "w") as f:
        f.write(text + "\n")
    print("\n" + text + "\n")

    # Apply boost to missed orgs (--apply-boost --push)
    missed = [r for r in rows if not r["contacted"]]
    if args.apply_boost:
        if not args.push:
            print(f"\nDRY RUN — would set falloff_flag=true on {len(missed)} non-contacted orgs.")
            print("    Re-run with --apply-boost --push to actually update HubSpot.")
        else:
            print(f"\n[PUSH] Setting falloff_flag=true on {len(missed)} orgs...")
            updates = [{"id": r["hs_id"], "properties": {HS_FIELDS["falloff_flag"]: "true"}}
                       for r in missed]
            sent = hubspot_client.batch_update_companies(updates)
            print(f"  Updated {sent} orgs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
