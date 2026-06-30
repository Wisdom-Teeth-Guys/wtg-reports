"""
SPOTIO → HubSpot backfill.

Pulls historical visit data from SPOTIO, matches leads to HubSpot companies by
address signature, classifies the most recent visit outcome, computes
consecutive-closed counts, and pushes the result to HubSpot custom fields.

Usage:
    python3 -m route_builder.spotio_backfill                              # dry-run, all leads with visits
    python3 -m route_builder.spotio_backfill --since 2026-01-01           # limit by date
    python3 -m route_builder.spotio_backfill --limit 50                   # process only first N matched leads
    python3 -m route_builder.spotio_backfill --push                       # actually write to HubSpot

Output:
    route_builder/output/backfill_YYYY-MM-DD/
        matched.csv         — leads matched + outcome computed
        unmatched.csv       — SPOTIO leads with visits we couldn't match to HubSpot
        ambiguous.csv       — SPOTIO leads with multiple HubSpot candidates
        summary.txt         — counts + top repeat-closed offices
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import address_matcher, hubspot_client, spotio_client
from .config import (
    CLOSED_OUTCOMES,
    CONTACTED_OUTCOMES,
    HS_FIELDS,
    NON_VISIT_RESULT_IDS,
    OUTPUT_DIR,
    SPOTIO_RESULT_TO_OUTCOME,
    SPOTIO_VISIT_TEMPLATE_ID,
    SUPPRESSED_OUTCOMES,
)


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------
def classify_visit(activity: dict) -> Optional[str]:
    """Return a HubSpot last_visit_outcome value for a SPOTIO visit activity,
    or None if it's not a real field visit (skip non-visit results)."""
    rid = str(activity.get("resultId") or "")
    if rid in NON_VISIT_RESULT_IDS:
        return None
    return SPOTIO_RESULT_TO_OUTCOME.get(rid)


def is_visit_activity(act: dict) -> bool:
    """True if this activity is a completed field visit."""
    return (
        act.get("type") == "event"
        and act.get("activityTemplateId") == SPOTIO_VISIT_TEMPLATE_ID
        and act.get("done") is True
    )


def parse_iso(s: str) -> Optional[datetime]:
    """Parse SPOTIO's ISO timestamps (handles trailing offset variations)."""
    if not s:
        return None
    # SPOTIO sometimes uses 7-digit fractional seconds; truncate to 6
    if "." in s:
        head, _, tail = s.partition(".")
        # tail may include timezone — strip the digit count to 6
        m = ""
        for ch in tail:
            if ch.isdigit() and len(m) < 6:
                m += ch
            elif ch.isdigit():
                continue
            else:
                # found tz marker
                tail = m + tail[tail.index(ch):]
                break
        else:
            tail = m
        s = head + "." + tail
    # Python's fromisoformat handles +00:00, +HH:MM, and Z if we replace Z
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def summarize_lead_visits(visits: list[dict]) -> dict:
    """Given a lead's visit activities (most recent first), compute the
    intelligence fields we push to HubSpot.

    Returns dict with keys:
        last_visit_outcome       (str or None)
        consecutive_closed_count (int)
        last_visit_date          (ISO date string yyyy-mm-dd or None)
        latest_classifiable      (bool)
    """
    # Sort newest first
    sorted_visits = sorted(
        visits,
        key=lambda v: parse_iso(v.get("date") or "") or datetime(1970, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )
    last_outcome = None
    consecutive = 0
    last_date = None
    saw_classifiable = False
    for v in sorted_visits:
        outcome = classify_visit(v)
        if outcome is None:
            # Phone call / mailed / unmapped — skip but don't break the chain
            continue
        saw_classifiable = True
        d = parse_iso(v.get("date") or "")
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
        "latest_classifiable": saw_classifiable,
    }


# ---------------------------------------------------------------------------
# Main backfill flow
# ---------------------------------------------------------------------------
def main(
    since: Optional[str] = None,
    limit: Optional[int] = None,
    push: bool = False,
    output_dir: Optional[str] = None,
) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    if output_dir is None:
        output_dir = os.path.join(OUTPUT_DIR, f"backfill_{today}")
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {output_dir}\n")

    # --- Phase 1: build HubSpot index ---
    print("[1/4] Fetching HubSpot companies and indexing by address signature...")
    t0 = time.time()
    hs_companies = list(hubspot_client.iter_companies(["name", "address", "city", "state", "zip"]))
    hs_index = address_matcher.build_hs_index(hs_companies)
    print(f"      Companies: {len(hs_companies):,}  |  signatures indexed: {len(hs_index):,}  |  {time.time()-t0:.1f}s\n")

    # --- Phase 2: walk SPOTIO leads, match each ---
    print("[2/4] Walking SPOTIO leads (sorted by lastActivityTime desc)...")
    matched_records = []      # (hs_id, hs_name, sp_lead, intel_dict)
    unmatched_records = []    # leads with visits we couldn't match
    ambiguous_records = []    # leads where multiple HS candidates match
    n_scanned = 0
    n_skipped_no_visits = 0
    n_skipped_old = 0

    since_dt = None
    if since:
        since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)

    for lead in spotio_client.iter_leads(page_size=100):
        n_scanned += 1
        if n_scanned % 1000 == 0:
            print(f"      ...scanned {n_scanned:,} leads (matched so far: {len(matched_records)})")
        vc = lead.get("visitsCount", 0) or 0
        if vc == 0:
            n_skipped_no_visits += 1
            continue
        # Optional date filter on lead's last activity time
        if since_dt:
            la = parse_iso(lead.get("lastActivityTime", ""))
            if la and la < since_dt:
                n_skipped_old += 1
                continue
        status, hs = address_matcher.find_match(lead, hs_index)
        if status == "unique":
            matched_records.append((hs, lead))
        elif status == "ambiguous":
            ambiguous_records.append(lead)
        else:
            unmatched_records.append(lead)
        if limit is not None and len(matched_records) >= limit:
            break

    print(f"      Scanned:    {n_scanned:,}")
    print(f"      No visits:  {n_skipped_no_visits:,}")
    print(f"      Too old:    {n_skipped_old:,}")
    print(f"      Matched:    {len(matched_records):,}")
    print(f"      Ambiguous:  {len(ambiguous_records):,}")
    print(f"      Unmatched:  {len(unmatched_records):,}\n")

    # --- Phase 3: fetch activities + classify ---
    print(f"[3/4] Fetching activities for {len(matched_records):,} matched leads...")
    intel_results = []  # (hs, lead, intel_dict)
    t1 = time.time()
    for i, (hs, lead) in enumerate(matched_records):
        if i and i % 50 == 0:
            elapsed = time.time() - t1
            rate = i / elapsed if elapsed else 0
            remaining = (len(matched_records) - i) / rate if rate else 0
            print(f"      {i:,}/{len(matched_records):,}  ({rate:.1f}/s, ~{remaining/60:.1f}min remaining)")
        try:
            acts = spotio_client.fetch_lead_activities(lead["id"])
        except Exception as e:
            print(f"      WARN failed to fetch activities for lead {lead['id']}: {e}")
            continue
        visits = [a for a in acts if is_visit_activity(a)]
        intel = summarize_lead_visits(visits)
        if intel["last_visit_outcome"] is None:
            # No classifiable visit found — skip
            continue
        intel_results.append((hs, lead, intel))
    print(f"      Classified: {len(intel_results):,} leads in {(time.time()-t1):.1f}s\n")

    # --- Phase 4: write CSVs and (optionally) push ---
    print("[4/4] Writing outputs...")
    matched_csv = os.path.join(output_dir, "matched.csv")
    with open(matched_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "hs_id", "hs_name", "hs_zip",
            "spotio_lead_id", "spotio_address", "spotio_visits_count",
            "last_visit_outcome", "consecutive_closed_count", "last_visit_date",
        ])
        for hs, lead, intel in intel_results:
            hp = hs.get("properties") or {}
            pin = lead.get("pin") or {}
            w.writerow([
                hs["id"], hp.get("name", ""), hp.get("zip", ""),
                lead["id"], pin.get("address", ""), lead.get("visitsCount", 0),
                intel["last_visit_outcome"],
                intel["consecutive_closed_count"],
                intel["last_visit_date"] or "",
            ])
    print(f"      matched.csv:    {len(intel_results):,} rows  →  {matched_csv}")

    unmatched_csv = os.path.join(output_dir, "unmatched.csv")
    with open(unmatched_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["spotio_lead_id", "address", "zip", "visits_count", "last_activity_time"])
        for lead in unmatched_records:
            pin = lead.get("pin") or {}
            w.writerow([
                lead["id"], pin.get("address", ""), pin.get("zip", ""),
                lead.get("visitsCount", 0), lead.get("lastActivityTime", ""),
            ])
    print(f"      unmatched.csv:  {len(unmatched_records):,} rows  →  {unmatched_csv}")

    ambiguous_csv = os.path.join(output_dir, "ambiguous.csv")
    with open(ambiguous_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["spotio_lead_id", "address", "zip", "visits_count"])
        for lead in ambiguous_records:
            pin = lead.get("pin") or {}
            w.writerow([
                lead["id"], pin.get("address", ""), pin.get("zip", ""),
                lead.get("visitsCount", 0),
            ])
    print(f"      ambiguous.csv:  {len(ambiguous_records):,} rows  →  {ambiguous_csv}")

    # Summary
    perm_closed = [r for r in intel_results if r[2]["last_visit_outcome"] in SUPPRESSED_OUTCOMES]
    high_consec = sorted(intel_results, key=lambda r: r[2]["consecutive_closed_count"], reverse=True)
    contacted = sum(1 for r in intel_results if r[2]["last_visit_outcome"] in CONTACTED_OUTCOMES)
    closed = sum(1 for r in intel_results if r[2]["last_visit_outcome"] in CLOSED_OUTCOMES)

    summary_lines = [
        f"SPOTIO Backfill Summary — {today}",
        f"",
        f"HubSpot companies:        {len(hs_companies):,}",
        f"  - with usable address:  {len(hs_index):,} unique signatures",
        f"",
        f"SPOTIO leads scanned:     {n_scanned:,}",
        f"  - no visits:            {n_skipped_no_visits:,}",
        f"  - filtered (--since):   {n_skipped_old:,}",
        f"  - matched to HubSpot:   {len(matched_records):,}",
        f"  - ambiguous:            {len(ambiguous_records):,}",
        f"  - unmatched:            {len(unmatched_records):,}",
        f"",
        f"Classified outcomes:      {len(intel_results):,}",
        f"  - last contacted:       {contacted:,}",
        f"  - last closed:          {closed:,}",
        f"  - permanently closed:   {len(perm_closed):,}",
        f"",
        f"Top 10 by consecutive_closed_count:",
    ]
    for hs, lead, intel in high_consec[:10]:
        if intel["consecutive_closed_count"] == 0:
            break
        hp = hs.get("properties") or {}
        summary_lines.append(
            f"  closed {intel['consecutive_closed_count']:>2}x in a row  "
            f"{hp.get('name', '')[:40]:<40}  {hp.get('zip', '')}  ({intel['last_visit_outcome']})"
        )

    summary_text = "\n".join(summary_lines)
    with open(os.path.join(output_dir, "summary.txt"), "w") as f:
        f.write(summary_text + "\n")
    print(f"\n{summary_text}\n")

    # --- Push to HubSpot ---
    if push:
        print(f"\n[PUSH] Updating {len(intel_results):,} companies in HubSpot...")
        updates = []
        for hs, lead, intel in intel_results:
            props = {
                HS_FIELDS["last_outcome"]: intel["last_visit_outcome"],
                HS_FIELDS["consec_closed"]: intel["consecutive_closed_count"],
            }
            if intel["last_visit_date"]:
                props[HS_FIELDS["last_visit"]] = intel["last_visit_date"]
            updates.append({"id": hs["id"], "properties": props})
        t = time.time()
        sent = hubspot_client.batch_update_companies(updates)
        print(f"       Pushed {sent:,} updates in {time.time()-t:.1f}s")
    else:
        print(f"\nDRY RUN — no HubSpot writes performed. Re-run with --push to apply.")
        print(f"           ({len(intel_results):,} updates would be sent)")

    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Backfill HubSpot visit intelligence from SPOTIO history")
    p.add_argument("--since", help="Only include leads with lastActivityTime >= this date (YYYY-MM-DD)")
    p.add_argument("--limit", type=int, help="Process only first N matched leads (for testing)")
    p.add_argument("--push", action="store_true", help="Actually write to HubSpot (default: dry-run)")
    p.add_argument("--output-dir", help="Override output directory")
    args = p.parse_args()
    sys.exit(main(since=args.since, limit=args.limit, push=args.push, output_dir=args.output_dir))
