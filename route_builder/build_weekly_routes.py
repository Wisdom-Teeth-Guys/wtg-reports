"""
MMC Weekly Route Builder — main orchestrator.

Pulls HubSpot companies, optionally pulls fresh visit intelligence from SPOTIO
(in-memory only, no HubSpot writes unless --push is given), scores every org,
buckets by territory, and writes a per-territory CSV with the top 30 priority
orgs for that week.

Default behavior is DRY-RUN — CSVs only, no HubSpot writes. Use --push to
actually stamp `visit_week_of` / `visit_priority_score` / `visit_reason` on the
selected companies (this is what MMC reads to build the daily routes).

Usage:
    # Dry-run, all territories, fresh SPOTIO intel:
    python3 -m route_builder.build_weekly_routes

    # Dry-run, one territory, faster (skip SPOTIO refresh):
    python3 -m route_builder.build_weekly_routes --territory "Dallas Southwest" --no-spotio

    # Push final assignments to HubSpot (lets MMC pick them up overnight):
    python3 -m route_builder.build_weekly_routes --push

    # Just check that HubSpot has all 15 custom fields, then exit:
    python3 -m route_builder.build_weekly_routes --check-fields
"""
import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from . import address_matcher, hubspot_client, spotio_client, territory_map
from .config import (
    CLOSED_OUTCOMES, CONTACTED_OUTCOMES, HS_FIELD_SCHEMA, HS_FIELDS,
    NON_VISIT_RESULT_IDS, ORGS_PER_REP, OUTPUT_DIR, OVERRIDES_DIR,
    SPOTIO_RESULT_TO_OUTCOME, SPOTIO_VISIT_TEMPLATE_ID,
)
from .scoring import OrgRecord, select_top_n, select_by_cadence


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def next_monday(reference: Optional[date] = None) -> date:
    """Return the next Monday strictly after `reference` (default: today)."""
    ref = reference or date.today()
    days_ahead = (7 - ref.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return ref + timedelta(days=days_ahead)


def parse_hs_date(s: Optional[str]) -> Optional[date]:
    """HubSpot returns dates as ISO8601 (date or datetime). Parse permissively."""
    if not s:
        return None
    s = s.strip().split("T")[0]
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HubSpot → OrgRecord
# ---------------------------------------------------------------------------
HS_FETCH_PROPERTIES = [
    "name", "address", "city", "state", "zip", "createdate",
    *HS_FIELDS.values(),
]


def hs_company_to_org(c: dict) -> OrgRecord:
    p = c.get("properties") or {}
    return OrgRecord(
        hs_id=c["id"],
        name=p.get("name", "") or "",
        zip=(p.get("zip") or "").strip()[:5],
        city=p.get("city", "") or "",
        state=p.get("state", "") or "",
        address=p.get("address", "") or "",
        tier=p.get(HS_FIELDS["tier_current"], "") or "",
        last_visit_date=parse_hs_date(p.get(HS_FIELDS["last_visit"])),
        last_won_date=parse_hs_date(p.get(HS_FIELDS["last_won"])),
        t12m_wins=int(p.get(HS_FIELDS["t12m_wins"]) or 0),
        lifetime_wins=int(float(p.get(HS_FIELDS["lifetime_wins"]) or 0)),
        falloff_flag=(p.get(HS_FIELDS["falloff_flag"]) == "true"),
        dormant_flag=(p.get(HS_FIELDS["dormant_flag"]) == "true"),
        create_date=parse_hs_date(p.get("createdate")),
        last_visit_outcome=p.get(HS_FIELDS["last_outcome"], "") or "",
        best_visit_window=p.get(HS_FIELDS["best_window"], "") or "",
        office_closes_for_lunch=(p.get(HS_FIELDS["lunch_closes"]) == "true"),
        office_closed_fridays=(p.get(HS_FIELDS["closed_fridays"]) == "true"),
        key_contact_name=p.get(HS_FIELDS["key_contact"], "") or "",
        consecutive_closed_count=int(p.get(HS_FIELDS["consec_closed"]) or 0),
    )


# ---------------------------------------------------------------------------
# Optional: in-memory SPOTIO intelligence refresh
# ---------------------------------------------------------------------------
def refresh_intel_from_spotio(orgs: list[OrgRecord], since: Optional[date] = None,
                               max_leads: Optional[int] = None) -> dict:
    """Walk SPOTIO leads, match to orgs by address, and overlay the most recent
    visit outcome into each matched OrgRecord (mutating in place). No HubSpot
    writes — purely in-memory.

    Returns a stats dict for the run summary.
    """
    print("  Refreshing visit intelligence from SPOTIO (in-memory only)...")
    t0 = time.time()
    # Build address index over the orgs we already have
    org_by_sig = {}
    for o in orgs:
        sig = address_matcher.address_signature(o.address, o.zip)
        if sig:
            org_by_sig.setdefault(sig, []).append(o)
    matched_orgs = 0
    activity_calls = 0
    scanned = 0
    since_dt = None
    if since:
        from datetime import timezone
        since_dt = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
    for lead in spotio_client.iter_leads(page_size=100):
        scanned += 1
        if max_leads and scanned > max_leads:
            break
        if (lead.get("visitsCount", 0) or 0) == 0:
            continue
        if since_dt:
            la_raw = lead.get("lastActivityTime", "")
            la = _parse_iso_safe(la_raw)
            if la and la < since_dt:
                continue
        pin = lead.get("pin") or {}
        sig = address_matcher.address_signature(pin.get("address"), pin.get("zip"))
        if not sig:
            continue
        cands = org_by_sig.get(sig)
        if not cands or len(cands) != 1:
            continue
        org = cands[0]
        try:
            acts = spotio_client.fetch_lead_activities(lead["id"])
            activity_calls += 1
        except Exception:
            continue
        visits = [a for a in acts
                  if a.get("type") == "event"
                  and a.get("activityTemplateId") == SPOTIO_VISIT_TEMPLATE_ID
                  and a.get("done")]
        if not visits:
            continue
        intel = _summarize_visits(visits)
        if intel["last_visit_outcome"] is None:
            continue
        # Overlay into the in-memory org
        org.last_visit_outcome = intel["last_visit_outcome"]
        org.consecutive_closed_count = intel["consecutive_closed_count"]
        if intel["last_visit_date"]:
            try:
                org.last_visit_date = date.fromisoformat(intel["last_visit_date"])
            except Exception:
                pass
        matched_orgs += 1
    print(f"    SPOTIO scan: {scanned:,} leads | matched & enriched: {matched_orgs:,} | "
          f"activity calls: {activity_calls:,} | {time.time()-t0:.1f}s")
    return {
        "spotio_scanned": scanned,
        "spotio_matched": matched_orgs,
        "spotio_activity_calls": activity_calls,
    }


def _parse_iso_safe(s: str):
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


def _summarize_visits(visits: list[dict]) -> dict:
    """Same logic as spotio_backfill.summarize_lead_visits (duplicated to avoid
    a circular import; keep them in sync)."""
    from datetime import timezone
    sorted_v = sorted(
        visits,
        key=lambda v: _parse_iso_safe(v.get("date", "")) or datetime(1970, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )
    last_outcome = None
    consecutive = 0
    last_date = None
    for v in sorted_v:
        rid = str(v.get("resultId") or "")
        if rid in NON_VISIT_RESULT_IDS:
            continue
        outcome = SPOTIO_RESULT_TO_OUTCOME.get(rid)
        if outcome is None:
            continue
        d = _parse_iso_safe(v.get("date", ""))
        if last_outcome is None:
            last_outcome = outcome
            last_date = d.date().isoformat() if d else None
        if outcome in CLOSED_OUTCOMES:
            consecutive += 1
        else:
            break
    return {
        "last_visit_outcome": last_outcome,
        "consecutive_closed_count": consecutive,
        "last_visit_date": last_date,
    }


# ---------------------------------------------------------------------------
# Overrides (manager exceptions)
# ---------------------------------------------------------------------------
def load_overrides(path: Optional[str], territory: str) -> tuple[set[str], set[str]]:
    """Returns (force_include_ids, exclude_ids) for a given territory."""
    if not path or not os.path.exists(path):
        return set(), set()
    force_in, exclude = set(), set()
    t_lower = territory.lower().strip()
    with open(path) as f:
        for row in csv.DictReader(f):
            if (row.get("territory", "") or "").lower().strip() != t_lower:
                continue
            action = (row.get("action", "") or "").upper().strip()
            hs_id = (row.get("hs_id", "") or "").strip()
            if not hs_id:
                continue
            if action == "FORCE_IN":
                force_in.add(hs_id)
            elif action == "EXCLUDE":
                exclude.add(hs_id)
    return force_in, exclude


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
CSV_COLUMNS = [
    "hs_id", "name", "address", "city", "state", "zip", "territory",
    "tier", "score", "visit_reason",
    "last_visit_date", "days_overdue", "last_visit_outcome",
    "consecutive_closed_count", "best_visit_window",
    "office_closes_for_lunch", "office_closed_fridays",
    "key_contact_name", "t12m_wins", "falloff_flag", "dormant_flag",
    "visit_week_of",
]


def safe_territory_filename(t: str) -> str:
    return t.replace(" ", "_").replace("/", "_").replace("-", "_") + ".csv"


def write_territory_csv(territory: str, orgs: list[OrgRecord], week_of: date,
                         out_dir: str) -> str:
    fp = os.path.join(out_dir, safe_territory_filename(territory))
    with open(fp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for o in orgs:
            w.writerow([
                o.hs_id, o.name, o.address, o.city, o.state, o.zip, territory,
                o.tier, o.score, o.visit_reason,
                o.last_visit_date.isoformat() if o.last_visit_date else "",
                o.days_overdue, o.last_visit_outcome,
                o.consecutive_closed_count, o.best_visit_window,
                "true" if o.office_closes_for_lunch else "",
                "true" if o.office_closed_fridays else "",
                o.key_contact_name, o.t12m_wins,
                "true" if o.falloff_flag else "",
                "true" if o.dormant_flag else "",
                week_of.isoformat(),
            ])
    return fp


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(args) -> int:
    today = date.today()
    week_of = date.fromisoformat(args.week_of) if args.week_of else next_monday(today)
    print(f"\n=== MMC Weekly Route Builder ===")
    print(f"  Today:           {today}")
    print(f"  Target week:     {week_of}  (Monday)\n")

    # Field check
    status = hubspot_client.check_custom_fields(HS_FIELD_SCHEMA)
    if status["missing"]:
        print(f"⚠️  HubSpot is missing {len(status['missing'])} custom fields:")
        for m in status["missing"]:
            print(f"    - {m}")
        print("\n    Run: python3 -m route_builder.setup_visit_fields --push\n")
        if args.check_fields:
            return 2
    elif args.check_fields:
        print("✓ All 15 HubSpot custom fields exist.")
        return 0

    # Load territory map
    print("[1/5] Loading territory → ZIP map...")
    t_map = territory_map.load_territory_zip_map()
    zip_to_territory = territory_map.load_zip_to_territory()
    target_territories = list(t_map.keys())
    if args.territory:
        target_territories = [args.territory.lower().strip()]
        if target_territories[0] not in t_map:
            print(f"  ✗ Territory '{args.territory}' not found. Available:")
            for t in sorted(t_map):
                print(f"    - {t}")
            return 1
    print(f"  Territories to process: {len(target_territories)}\n")

    # Fetch HubSpot companies
    print("[2/5] Fetching HubSpot companies (this can take ~1 minute)...")
    t0 = time.time()
    raw = list(hubspot_client.iter_companies(HS_FETCH_PROPERTIES))
    orgs = [hs_company_to_org(c) for c in raw]
    print(f"  {len(orgs):,} companies fetched in {time.time()-t0:.1f}s\n")

    # Attach territory from zip
    for o in orgs:
        o.territory = zip_to_territory.get(o.zip, "")

    # Refresh visit intel from SPOTIO (in-memory)
    spotio_stats = {}
    if not args.no_spotio:
        print("[3/5] Refreshing visit intelligence from SPOTIO...")
        since = date.fromisoformat(args.spotio_since) if args.spotio_since else (today - timedelta(days=180))
        spotio_stats = refresh_intel_from_spotio(orgs, since=since, max_leads=args.spotio_max_leads)
        print()
    else:
        print("[3/5] Skipping SPOTIO refresh (--no-spotio).\n")

    # Score + select per territory
    print("[4/5] Scoring and selecting top orgs per territory...")
    out_dir = args.output_dir or os.path.join(OUTPUT_DIR, f"routes_{week_of.isoformat()}")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    all_selected = []
    summary_lines = []
    for t_lower in target_territories:
        canonical = next((zip_to_territory[z] for z in t_map[t_lower] if z in zip_to_territory),
                         t_lower.title())
        t_orgs = [o for o in orgs if o.zip in t_map[t_lower]]
        if not t_orgs:
            summary_lines.append(f"  {canonical:<25}  (no orgs in HubSpot for this territory)")
            continue
        # Overrides
        overrides_path = args.overrides
        if not overrides_path:
            default_override = os.path.join(OVERRIDES_DIR, f"overrides_{week_of.isoformat()}.csv")
            if os.path.exists(default_override):
                overrides_path = default_override
        force_in, exclude = load_overrides(overrides_path, canonical)
        # Score + select (cadence-based: tier rules first, then T4 fill, then Zero)
        selected = select_by_cadence(t_orgs, ORGS_PER_REP, today,
                                     force_include_ids=force_in, exclude_ids=exclude)
        # Annotate territory for output
        for o in selected:
            o.territory = canonical
        write_territory_csv(canonical, selected, week_of, out_dir)
        all_selected.extend(selected)
        # Summary line
        scored_active = sum(1 for o in selected if o.score > 0)
        from .config import SUPPRESSED_OUTCOMES
        suppressed = sum(1 for o in t_orgs if o.last_visit_outcome in SUPPRESSED_OUTCOMES)
        summary_lines.append(
            f"  {canonical:<25}  pool={len(t_orgs):>4}  "
            f"selected={len(selected):>3}  scored>0={scored_active:>3}  "
            f"suppressed={suppressed:>3}"
        )
    print("\nPer-territory summary:")
    for line in summary_lines:
        print(line)

    # Write combined summary
    summary_file = os.path.join(out_dir, "_summary.txt")
    with open(summary_file, "w") as f:
        f.write(f"MMC Weekly Route Builder — {today.isoformat()}\n")
        f.write(f"Target week: {week_of.isoformat()} (Monday)\n\n")
        for line in summary_lines:
            f.write(line + "\n")
        f.write(f"\nTotal selected: {len(all_selected)}\n")
        if spotio_stats:
            f.write(f"\nSPOTIO refresh: scanned={spotio_stats.get('spotio_scanned', 0)} "
                    f"matched={spotio_stats.get('spotio_matched', 0)}\n")

    print(f"\n[5/5] Output: {out_dir}")
    print(f"  Total orgs selected across all territories: {len(all_selected)}")

    # Assign each selected org to a Mon-Fri day-bucket (geographic + open-days aware)
    day_assignments = assign_to_days(all_selected, week_of)
    # Log per-territory day counts
    per_terr_days: dict = defaultdict(lambda: {"Mon": 0, "Tue": 0, "Wed": 0, "Thu": 0, "Fri": 0})
    for o in all_selected:
        d = day_assignments.get(o.hs_id)
        if d:
            per_terr_days[o.territory][["Mon", "Tue", "Wed", "Thu", "Fri"][(d - week_of).days]] += 1
    print(f"\nDay-of-week split per territory:")
    for terr in sorted(per_terr_days):
        c = per_terr_days[terr]
        print(f"  {terr:26s}  Mon={c['Mon']}  Tue={c['Tue']}  Wed={c['Wed']}  Thu={c['Thu']}  Fri={c['Fri']}")

    # Push to HubSpot
    if args.push:
        print(f"\n[PUSH] Writing visit_week_of + visit_monday..friday to HubSpot...")
        # 1. Clear any leftover per-day stamps from previous week
        cleared = _clear_stale_day_stamps(week_of)
        if cleared:
            print(f"  Cleared per-day stamps on {cleared} stale companies from prior weeks")

        # 2. Set new week + day fields on the selected orgs
        day_field_by_weekday = {
            0: HS_FIELDS["day_monday"],
            1: HS_FIELDS["day_tuesday"],
            2: HS_FIELDS["day_wednesday"],
            3: HS_FIELDS["day_thursday"],
            4: HS_FIELDS["day_friday"],
        }
        updates = []
        for o in all_selected:
            assigned = day_assignments.get(o.hs_id)
            props = {
                HS_FIELDS["week_of"]:        week_of.isoformat(),
                HS_FIELDS["priority_score"]: o.score,
                HS_FIELDS["visit_reason"]:   o.visit_reason[:255],
                # Clear all 5 day fields, then set the assigned one below
                HS_FIELDS["day_monday"]:    "",
                HS_FIELDS["day_tuesday"]:   "",
                HS_FIELDS["day_wednesday"]: "",
                HS_FIELDS["day_thursday"]:  "",
                HS_FIELDS["day_friday"]:    "",
            }
            if assigned:
                props[day_field_by_weekday[(assigned - week_of).days]] = assigned.isoformat()
            updates.append({"id": o.hs_id, "properties": props})
        t = time.time()
        sent = hubspot_client.batch_update_companies(updates)
        print(f"  Pushed {sent:,} updates in {time.time()-t:.1f}s")
    else:
        print(f"\nDRY RUN — no HubSpot writes. Re-run with --push to stamp visit_week_of + "
              f"visit_<day> on the {len(all_selected)} selected orgs.")

    return 0


# ---------------------------------------------------------------------------
# Daily bucketing (Mon-Fri) — geographic + open-days-aware
# ---------------------------------------------------------------------------
def assign_to_days(orgs: list[OrgRecord], week_monday: date) -> dict[str, date]:
    """Split orgs into 5 daily buckets. Returns {hs_id: date}.

    Algorithm:
      1. Group orgs by territory (so each marketer's week is self-contained).
      2. Within each territory, sort by zip (geographic clustering proxy).
      3. Chunk into 5 groups of ~equal size (usually 10 each for a 50-target).
      4. Bump `office_closed_fridays=true` offices out of the Friday bucket
         into the smallest of the Mon-Thu buckets.
      5. Assign the corresponding day-of-week date to each org.
    """
    import math
    assignment: dict[str, date] = {}
    by_territory: dict[str, list[OrgRecord]] = defaultdict(list)
    for o in orgs:
        by_territory[o.territory or "_none"].append(o)

    days = [week_monday + timedelta(days=i) for i in range(5)]  # Mon..Fri

    for _terr, terr_orgs in by_territory.items():
        # Geographic-ish sort by zip
        sorted_orgs = sorted(terr_orgs, key=lambda o: (o.zip or "99999", o.hs_id))
        n = len(sorted_orgs)
        chunk_size = max(1, math.ceil(n / 5))
        buckets: list[list[OrgRecord]] = [
            sorted_orgs[i * chunk_size:(i + 1) * chunk_size] for i in range(5)
        ]
        # Bump closed-Fridays offices out of the Friday bucket (index 4)
        friday = buckets[4]
        closed_fri = [o for o in friday if o.office_closed_fridays]
        if closed_fri:
            buckets[4] = [o for o in friday if not o.office_closed_fridays]
            for o in closed_fri:
                # Move to the smallest of Mon-Thu buckets
                idx = min(range(4), key=lambda i: len(buckets[i]))
                buckets[idx].append(o)
        # Assign
        for i, bucket in enumerate(buckets):
            for o in bucket:
                assignment[o.hs_id] = days[i]
    return assignment


def _clear_stale_day_stamps(this_week_monday: date) -> int:
    """Clear visit_monday..visit_friday on companies whose visit_week_of is NOT
    this week's Monday but still has a per-day stamp lingering. Returns count."""
    import urllib.parse
    # HubSpot search: companies where visit_week_of != this_monday AND any of the
    # 5 day fields IS NOT EMPTY. Cheapest robust approach: search each day field.
    day_field_names = [HS_FIELDS[k] for k in
                       ("day_monday", "day_tuesday", "day_wednesday", "day_thursday", "day_friday")]
    stale_ids: set = set()
    this_monday_iso = this_week_monday.isoformat()
    import datetime as _dt
    this_monday_ms = int(_dt.datetime.fromisoformat(this_monday_iso)
                         .replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
    for field in day_field_names:
        # Paginate through matches
        after = None
        while True:
            body = {
                "filterGroups": [{"filters": [
                    {"propertyName": field, "operator": "HAS_PROPERTY"},
                    {"propertyName": HS_FIELDS["week_of"], "operator": "NEQ", "value": this_monday_ms},
                ]}],
                "properties": [field],
                "limit": 100,
            }
            if after: body["after"] = after
            try:
                d = hubspot_client.hs_post("/crm/v3/objects/companies/search", body)
            except Exception:
                break
            for r in d.get("results", []): stale_ids.add(r["id"])
            after = (d.get("paging") or {}).get("next", {}).get("after")
            if not after: break
    if not stale_ids:
        return 0
    # Clear all 5 day fields on each stale record
    clear_props = {name: "" for name in day_field_names}
    updates = [{"id": hs_id, "properties": clear_props} for hs_id in stale_ids]
    hubspot_client.batch_update_companies(updates)
    return len(stale_ids)


def build_argparser():
    p = argparse.ArgumentParser(description="MMC Weekly Route Builder")
    p.add_argument("--push", action="store_true",
                   help="Actually write visit_week_of / score / reason to HubSpot (default: dry-run)")
    p.add_argument("--territory", help="Process only this territory (default: all 15)")
    p.add_argument("--week-of", help="Override target Monday (YYYY-MM-DD). Default: next Monday")
    p.add_argument("--output-dir", help="Override CSV output directory")
    p.add_argument("--overrides", help="Path to manager override CSV (default: route_builder/overrides/overrides_{week_of}.csv if present)")
    p.add_argument("--no-spotio", action="store_true",
                   help="Skip SPOTIO intel refresh (use whatever's already in HubSpot)")
    p.add_argument("--spotio-since", help="Only pull SPOTIO leads with lastActivityTime >= date (default: 180 days ago)")
    p.add_argument("--spotio-max-leads", type=int,
                   help="Cap on SPOTIO leads scanned (for fast testing)")
    p.add_argument("--check-fields", action="store_true",
                   help="Verify HubSpot custom fields exist, then exit")
    return p


if __name__ == "__main__":
    args = build_argparser().parse_args()
    sys.exit(main(args))
