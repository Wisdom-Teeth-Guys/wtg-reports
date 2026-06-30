"""
Monthly tier recalculation.

Reads a Pipedrive deal export (or any CSV/XLSX of won deals), computes T12M
wins per org, classifies each org into VIP/T1/T2/T3/Zero using a two-pass rule:

  Pass 1 — Absolute (wins-based):
      VIP = 20+ T12M wins
      T1  = 11–19
      T2  = 5–10
      T3  = 1–4
      Zero = 0

  Pass 2 — Per-market percentile (fills thin markets):
      For each territory, rank orgs by T12M wins descending. Then:
          top 5%    → VIP
          5–15%     → T1
          15–40%    → T2
          40–70%    → T3
          rest      → Zero
      Only orgs with at least 1 T12M win are eligible for percentile promotion.

  Final tier = MAX(absolute, percentile) — i.e. you always get the BETTER of
  the two. Thin markets (e.g. Tucson, where no one hits 20 absolute wins) still
  end up with a real VIP via the percentile pass; strong markets are unaffected.

Three-way (wins / refs / weighted) comparison is retired — wins-only.

Usage:
    python3 -m route_builder.tier_refresh --pipedrive-file path/to/deals.csv
    python3 -m route_builder.tier_refresh --pipedrive-file deals.csv --push
"""
import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from . import hubspot_client
from .config import HS_EXISTING_FIELDS, HS_FIELDS, OUTPUT_DIR


# Tier thresholds (absolute, wins-based — Pass 1)
TIER_THRESHOLDS = [
    ("VIP", 20),
    ("T1", 11),
    ("T2", 5),
    ("T3", 1),
]

# Per-market percentile bands (Pass 2 fallback for thin markets).
# Format: (tier_name, top_pct_cutoff). Each band runs from the previous cutoff
# to this one. Eligible only if T12M wins >= 1.
TIER_PERCENTILES = [
    ("VIP",  0.05),
    ("T1",   0.15),
    ("T2",   0.40),
    ("T3",   0.70),
]

# Rank order — higher index = higher tier. Used to pick the better tier between
# absolute and percentile passes.
TIER_RANK = {"Zero": 0, "T3": 1, "T2": 2, "T1": 3, "VIP": 4}


def assign_tier_absolute(t12m_wins: int) -> str:
    """Pass 1 — strict wins-based thresholds."""
    for tier, threshold in TIER_THRESHOLDS:
        if t12m_wins >= threshold:
            return tier
    return "Zero"


def assign_tier_percentile(rank: int, total_with_wins: int) -> str:
    """Pass 2 — given an org's 0-indexed rank within its market (sorted by
    T12M wins desc, only orgs with >=1 win counted), return its percentile tier.

    rank=0 is the top org in the market. In a 1-org market, that org gets VIP.
    """
    if total_with_wins <= 0:
        return "Zero"
    pct = rank / total_with_wins  # 0.0 for top rank, (N-1)/N for last
    for tier, cutoff in TIER_PERCENTILES:
        if pct < cutoff:
            return tier
    return "Zero"


def best_tier(*tiers: str) -> str:
    """Return the highest-ranked tier among the inputs."""
    return max(tiers, key=lambda t: TIER_RANK.get(t, -1))


# Kept for backward compat — anything still calling assign_tier() gets absolute.
assign_tier = assign_tier_absolute


def _norm_name(s: str) -> str:
    """Normalize org name for fuzzy matching."""
    if not s:
        return ""
    s = s.lower().strip()
    # Drop common suffixes
    for suffix in [" - phoenix", " - dallas", ", inc", ", llc", " dds", " dental", " orthodontics"]:
        s = s.replace(suffix, "")
    # Strip punctuation and collapse whitespace
    import re
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def load_pipedrive_wins(path: str, today: Optional[date] = None) -> dict:
    """Read a Pipedrive deal export and return {normalized_org_name: {t12m_wins, last_won_date}}.

    EXPECTED COLUMNS (adjust once we see the real export):
        Organization, Won Date, Deal Value
        OR similar — common alternatives:
        Org Name, Close Date, Amount

    Returns {} if file doesn't exist or can't be parsed.
    """
    if not os.path.exists(path):
        print(f"  ✗ File not found: {path}")
        return {}

    today = today or date.today()
    window_start = today - timedelta(days=365)

    # Detect file type
    ext = path.lower().split(".")[-1]
    if ext == "xlsx":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return {}
            headers = [str(h or "").strip() for h in rows[0]]
            data_rows = [dict(zip(headers, r)) for r in rows[1:]]
        except ImportError:
            print("  ✗ openpyxl not installed — install with: pip install openpyxl")
            return {}
    else:
        with open(path) as f:
            data_rows = list(csv.DictReader(f))

    if not data_rows:
        print(f"  ✗ No data rows in {path}")
        return {}

    headers = list(data_rows[0].keys())
    print(f"  Loaded {len(data_rows):,} rows. Columns: {headers}")

    # Auto-detect column names (case-insensitive)
    lower_headers = {h.lower(): h for h in headers}
    def find_col(*candidates):
        for c in candidates:
            for lh, h in lower_headers.items():
                if c in lh:
                    return h
        return None

    name_col = find_col("organization", "org name", "company", "account")
    date_col = find_col("won date", "close date", "deal won", "date won")
    if not name_col or not date_col:
        print(f"  ✗ Could not find required columns. Need: organization name, won date.")
        print(f"     Available: {headers}")
        return {}
    print(f"  Using columns: name='{name_col}', date='{date_col}'")

    by_org = defaultdict(lambda: {"t12m_wins": 0, "last_won_date": None})
    skipped_old = 0
    skipped_no_date = 0
    for row in data_rows:
        org_name = (row.get(name_col) or "").strip()
        if not org_name:
            continue
        raw_d = row.get(date_col)
        won_date = None
        if isinstance(raw_d, datetime):
            won_date = raw_d.date()
        elif isinstance(raw_d, date):
            won_date = raw_d
        elif isinstance(raw_d, str) and raw_d:
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
                try:
                    won_date = datetime.strptime(raw_d.strip(), fmt).date()
                    break
                except Exception:
                    pass
        if not won_date:
            skipped_no_date += 1
            continue
        norm = _norm_name(org_name)
        entry = by_org[norm]
        # T12M counter
        if won_date >= window_start:
            entry["t12m_wins"] += 1
        else:
            skipped_old += 1
        # Last won date
        if entry["last_won_date"] is None or won_date > entry["last_won_date"]:
            entry["last_won_date"] = won_date

    print(f"  Parsed: {len(by_org)} unique orgs, skipped {skipped_no_date} (no date), "
          f"{skipped_old} deals outside T12M window")
    return dict(by_org)


def match_to_hubspot(pipedrive_data: dict, hs_companies: list[dict]) -> tuple:
    """Match Pipedrive orgs to HubSpot companies by normalized name, then assign
    tiers using BOTH passes (absolute + per-market percentile).

    Returns (matches, unmatched_pipedrive_keys) where each match is:
        {"hs_id", "name", "territory", "t12m_wins", "last_won_date",
         "tier_absolute", "tier_percentile", "tier"}
    """
    pd_by_norm = pipedrive_data
    matches = []
    unmatched_pd_keys = set(pd_by_norm.keys())
    territory_field = HS_EXISTING_FIELDS["territory"]
    for c in hs_companies:
        p = c.get("properties") or {}
        norm = _norm_name(p.get("name", ""))
        if not norm:
            continue
        info = pd_by_norm.get(norm)
        if not info:
            continue
        unmatched_pd_keys.discard(norm)
        matches.append({
            "hs_id":         c["id"],
            "name":          p.get("name"),
            "territory":     (p.get(territory_field) or "").strip() or "(unknown)",
            "t12m_wins":     info["t12m_wins"],
            "last_won_date": info["last_won_date"].isoformat() if info["last_won_date"] else "",
        })

    # Pass 1: absolute tier per org
    for m in matches:
        m["tier_absolute"] = assign_tier_absolute(m["t12m_wins"])

    # Pass 2: percentile tier — ranked within each territory
    by_territory: dict[str, list[dict]] = defaultdict(list)
    for m in matches:
        by_territory[m["territory"]].append(m)
    for territory, orgs in by_territory.items():
        # Rank by T12M wins desc; only orgs with >=1 win are eligible
        with_wins = sorted([o for o in orgs if o["t12m_wins"] >= 1],
                           key=lambda o: o["t12m_wins"], reverse=True)
        total = len(with_wins)
        for rank, o in enumerate(with_wins):
            o["tier_percentile"] = assign_tier_percentile(rank, total)
        for o in orgs:
            o.setdefault("tier_percentile", "Zero")

    # Final tier = better of the two
    for m in matches:
        m["tier"] = best_tier(m["tier_absolute"], m["tier_percentile"])

    print(f"  Matched {len(matches)} of {len(hs_companies):,} HubSpot companies")
    if unmatched_pd_keys:
        print(f"  Pipedrive orgs not found in HubSpot: {len(unmatched_pd_keys)} (see unmatched.csv)")

    # Quick per-market tier-fill diagnostic
    promoted_by_percentile = sum(1 for m in matches if m["tier"] != m["tier_absolute"])
    if promoted_by_percentile:
        print(f"  Per-market percentile promoted {promoted_by_percentile} orgs above their absolute tier")

    return matches, list(unmatched_pd_keys)


def main():
    p = argparse.ArgumentParser(description="Recompute org tiers from Pipedrive deal data")
    p.add_argument("--pipedrive-file", required=True,
                   help="Path to Pipedrive export (CSV or XLSX)")
    p.add_argument("--push", action="store_true",
                   help="Actually write to HubSpot (default: dry-run)")
    p.add_argument("--output-dir", help="Override output directory")
    args = p.parse_args()

    today = date.today()
    out_dir = args.output_dir or os.path.join(OUTPUT_DIR, f"tier_refresh_{today.isoformat()}")
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    print(f"\n=== Tier Refresh — {today} ===\n")
    print(f"[1/3] Loading Pipedrive deals from {args.pipedrive_file}...")
    pd_data = load_pipedrive_wins(args.pipedrive_file, today=today)
    if not pd_data:
        print("Nothing to do.")
        return 1

    print(f"\n[2/3] Fetching HubSpot companies (with territory)...")
    hs_companies = list(hubspot_client.iter_companies(
        ["name", "zip", "city", "state", HS_EXISTING_FIELDS["territory"],
         HS_FIELDS["tier_current"]]
    ))
    print(f"  {len(hs_companies):,} companies")

    matches, unmatched = match_to_hubspot(pd_data, hs_companies)

    # For each matched org, look up its current tier (so we can stamp tier_previous on changes)
    current_tier_by_id = {
        c["id"]: ((c.get("properties") or {}).get(HS_FIELDS["tier_current"]) or "")
        for c in hs_companies
    }
    for m in matches:
        m["tier_was"] = current_tier_by_id.get(m["hs_id"], "")

    # Per-territory + overall tier distribution preview
    from collections import Counter
    dist = Counter(m["tier"] for m in matches)
    print(f"\n  Overall tier distribution (final):")
    for tier in ["VIP", "T1", "T2", "T3", "Zero"]:
        print(f"    {tier:<5} {dist.get(tier, 0):>5}")

    abs_dist = Counter(m["tier_absolute"] for m in matches)
    pct_dist = Counter(m["tier_percentile"] for m in matches)
    print(f"\n  Pass comparison:")
    print(f"    {'tier':<5}  {'absolute':>8}  {'percentile':>10}  {'final':>6}")
    for tier in ["VIP", "T1", "T2", "T3", "Zero"]:
        print(f"    {tier:<5}  {abs_dist.get(tier, 0):>8}  {pct_dist.get(tier, 0):>10}  {dist.get(tier, 0):>6}")

    # Per-territory breakdown
    by_terr = defaultdict(lambda: Counter())
    for m in matches:
        by_terr[m["territory"]][m["tier"]] += 1
    print(f"\n  Per-territory final distribution:")
    for terr in sorted(by_terr):
        c = by_terr[terr]
        line = f"    {terr:<25} " + "  ".join(f"{t}={c.get(t, 0):>3}" for t in ["VIP", "T1", "T2", "T3"])
        print(line)

    # Write CSVs
    matches_csv = os.path.join(out_dir, "tier_assignments.csv")
    with open(matches_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "hs_id", "name", "territory", "t12m_wins", "last_won_date",
            "tier_was", "tier_absolute", "tier_percentile", "tier",
        ])
        w.writeheader()
        w.writerows(matches)
    print(f"\n  tier_assignments.csv: {matches_csv}")

    unmatched_csv = os.path.join(out_dir, "unmatched.csv")
    with open(unmatched_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pipedrive_normalized_name", "t12m_wins", "last_won_date"])
        for k in unmatched:
            info = pd_data[k]
            w.writerow([k, info["t12m_wins"],
                        info["last_won_date"].isoformat() if info["last_won_date"] else ""])
    print(f"  unmatched.csv:        {unmatched_csv}")

    # Identify tier drops (VIP/T1 only matter for Band 1 of the prioritization spec,
    # but we track every drop)
    drops = [m for m in matches
             if m["tier_was"] and TIER_RANK.get(m["tier"], 0) < TIER_RANK.get(m["tier_was"], 0)]
    promos = [m for m in matches
              if m["tier_was"] and TIER_RANK.get(m["tier"], 0) > TIER_RANK.get(m["tier_was"], 0)]
    print(f"\n  Tier changes: {len(drops)} drops, {len(promos)} promotions")
    for d in drops[:10]:
        print(f"    DROP   {d['tier_was']:>3} → {d['tier']:<3}  {d['name']}  ({d['territory']})")

    # Push
    if args.push:
        print(f"\n[3/3] [PUSH] Updating {len(matches)} HubSpot orgs...")
        today_iso = today.isoformat()
        updates = []
        for m in matches:
            props = {
                HS_FIELDS["tier_current"]: m["tier"],
                HS_FIELDS["t12m_wins"]:    m["t12m_wins"],
            }
            if m["last_won_date"]:
                props[HS_FIELDS["last_won"]] = m["last_won_date"]
            # If the tier dropped this run, stamp tier_previous + tier_dropped_date
            # so Band 1 (tier-dropped, no diagnostic) can surface this office next week.
            tier_was = m["tier_was"]
            if tier_was and TIER_RANK.get(m["tier"], 0) < TIER_RANK.get(tier_was, 0):
                props[HS_FIELDS["tier_previous"]] = tier_was
                props[HS_FIELDS["tier_dropped_date"]] = today_iso
                # New tier-drop event resets the diagnostic flag so a fresh
                # diagnostic visit can be logged.
                props[HS_FIELDS["diagnostic_logged"]] = "false"
            updates.append({"id": m["hs_id"], "properties": props})
        sent = hubspot_client.batch_update_companies(updates)
        print(f"  Pushed {sent} updates")
    else:
        print(f"\n[3/3] DRY RUN — no HubSpot writes. Re-run with --push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
