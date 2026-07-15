"""Stamp HubSpot companies with mmc_visit_recency_bucket based on last_visit_date.

Drives the MMC "Create Group" auto-grouping feature. Once you enable
"Create Group" on this field in MMC's HubSpot integration mapping, MMC will
auto-create + maintain three account groups:

    Inactive 30-59 days
    Inactive 60-89 days
    Inactive 90+ days

Bucketing rule (non-overlapping):
    days since last visit       → bucket value
    ---------------------       --------------
    < 30 days  (recent)         → (unset; no group)
    30-59 days                  → inactive_30_days
    60-89 days                  → inactive_60_days
    90+ days OR never visited   → inactive_90_plus_days

Dry-run by default. Use --push to actually create the property and PATCH companies.

Usage:
    python3 -m route_builder.mmc_recency_buckets             # dry-run preview
    python3 -m route_builder.mmc_recency_buckets --push      # actually push
    python3 -m route_builder.mmc_recency_buckets --skip-property-create --push   # skip property setup, only refresh values
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from collections import Counter

from . import hubspot_client

PROP_NAME = "mmc_visit_recency_bucket"
PROP_LABEL = "MMC Visit Recency Bucket"
BUCKETS = [
    ("inactive_30_days",       "Inactive 30-59 days"),
    ("inactive_60_days",       "Inactive 60-89 days"),
    ("inactive_90_plus_days",  "Inactive 90+ days"),
]


def bucket_for(days: int | None) -> str | None:
    """Return the bucket value for a given days-since-last-visit, or None for unset.

    Companies without any last_visit_date stay unset — we don't bucket
    'never visited' offices, only ones with real Spotio visit history that's gone stale.
    """
    if days is None:
        return None
    if days < 30:
        return None
    if days < 60:
        return "inactive_30_days"
    if days < 90:
        return "inactive_60_days"
    return "inactive_90_plus_days"


def days_since(date_str: str | None, today: dt.date) -> int | None:
    if not date_str:
        return None
    try:
        d = dt.date.fromisoformat(date_str[:10])
    except Exception:
        return None
    return (today - d).days


def ensure_property(push: bool) -> bool:
    """Create the property if it doesn't exist. Returns True if it now exists."""
    existing = {p["name"]: p for p in hubspot_client.fetch_company_properties()}
    if PROP_NAME in existing:
        print(f"  Property {PROP_NAME!r} already exists.", file=sys.stderr)
        return True
    print(f"  Property {PROP_NAME!r} does NOT exist yet.", file=sys.stderr)
    if not push:
        print(f"  (--push not set; would create with options: {[v for v,_ in BUCKETS]})", file=sys.stderr)
        return False
    hubspot_client.create_company_property(
        name=PROP_NAME,
        label=PROP_LABEL,
        type_="enumeration",
        field_type="select",
        options=[v for v, _ in BUCKETS],
        group_name="companyinformation",
    )
    print(f"  Created {PROP_NAME!r} with options {[v for v,_ in BUCKETS]}", file=sys.stderr)
    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--push", action="store_true", help="Actually create property + PATCH companies.")
    p.add_argument("--skip-property-create", action="store_true",
                   help="Skip the property-create step (assumes property exists).")
    args = p.parse_args()

    print("[1/3] Ensuring property exists...", file=sys.stderr)
    if not args.skip_property_create:
        ok = ensure_property(args.push)
        if not ok and args.push:
            print("ERROR: property creation failed", file=sys.stderr); return 2

    print("[2/3] Streaming all HubSpot companies (need last_visit_date + current bucket)...",
          file=sys.stderr)
    today = dt.datetime.utcnow().date()
    bucket_distribution = Counter()
    changes = []  # list of (hs_id, name, old_bucket, new_bucket, days)
    n_scanned = 0

    for c in hubspot_client.iter_companies(["name", "last_visit_date", PROP_NAME]):
        n_scanned += 1
        props = c.get("properties") or {}
        days = days_since(props.get("last_visit_date"), today)
        new_bucket = bucket_for(days)
        old_bucket = props.get(PROP_NAME) or None
        bucket_distribution[new_bucket or "(unset)"] += 1
        if new_bucket != old_bucket:
            changes.append((c["id"], props.get("name"), old_bucket, new_bucket, days))
        if n_scanned % 5000 == 0:
            print(f"  ...scanned {n_scanned:,}", file=sys.stderr)

    print(f"\nScanned {n_scanned:,} companies.", file=sys.stderr)
    print("Bucket distribution (after this run):", file=sys.stderr)
    for k, v in sorted(bucket_distribution.items(), key=lambda x: -x[1]):
        print(f"  {k:30s} {v:,}", file=sys.stderr)
    print(f"\nChanges needed: {len(changes):,}", file=sys.stderr)
    if changes[:5]:
        print("First 5 changes preview:", file=sys.stderr)
        for hs_id, name, old, new, days in changes[:5]:
            print(f"  hs={hs_id} days={days} {old!r} -> {new!r}  ({name!r})", file=sys.stderr)

    if not args.push:
        print("\n(dry-run; not patching. add --push to apply)", file=sys.stderr)
        return 0

    print(f"\n[3/3] PATCHing {len(changes):,} companies...", file=sys.stderr)
    t0 = time.time()
    ok = fail = 0
    for i, (hs_id, name, old, new, days) in enumerate(changes):
        body = {"properties": {PROP_NAME: new or ""}}
        try:
            hubspot_client.hs_patch(f"/crm/v3/objects/companies/{hs_id}", body)
            ok += 1
        except Exception as e:
            fail += 1
            if fail < 10:
                print(f"  fail hs={hs_id}: {e}", file=sys.stderr)
        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed else 0
            eta = (len(changes) - i - 1) / rate if rate else 0
            print(f"  {i+1:,}/{len(changes):,}  ok={ok} fail={fail}  ~{eta/60:.1f}min remain",
                  file=sys.stderr)
    print(f"\nDone: ok={ok}  fail={fail}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
