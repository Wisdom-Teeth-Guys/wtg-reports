"""
Create the 15 HubSpot custom fields needed for the Route Builder + Visit
Intelligence layer.

Usage:
    python3 -m route_builder.setup_visit_fields                # dry-run by default
    python3 -m route_builder.setup_visit_fields --push         # actually create missing fields
    python3 -m route_builder.setup_visit_fields --check        # just report status; no probing

Safe to run repeatedly: existing fields are skipped (idempotent).
"""
import argparse
import sys
from typing import Optional

from .config import HS_FIELD_SCHEMA
from .hubspot_client import (
    check_custom_fields,
    create_company_property,
    fetch_company_properties,
)


# Fields whose names contain any of these substrings may already be served by
# MMC/SPOTIO native sync (e.g. a "last_check_in" field).  We surface them so the
# operator can map to existing data instead of duplicating.
KEYWORD_CONFLICTS = ("visit", "tier", "last_won", "wins", "falloff", "dormant",
                     "check", "outcome", "office", "contact")


def _label_for(slug: str) -> str:
    return slug.replace("_", " ").title()


def find_potential_conflicts() -> list[dict]:
    """Return existing HubSpot company properties whose names suggest overlap."""
    existing = fetch_company_properties()
    hits = []
    schema_names = {entry[0] for entry in HS_FIELD_SCHEMA}
    for prop in existing:
        name_lower = prop["name"].lower()
        # Skip if it IS one of our planned fields (already-created — that's fine)
        if prop["name"] in schema_names:
            continue
        if any(kw in name_lower for kw in KEYWORD_CONFLICTS):
            hits.append(prop)
    return hits


def main(push: bool = False, check_only: bool = False) -> int:
    status = check_custom_fields(HS_FIELD_SCHEMA)
    total = len(HS_FIELD_SCHEMA)
    print(f"\n=== HubSpot Custom Field Status ===")
    print(f"  Existing: {len(status['existing'])} / {total}")
    print(f"  Missing:  {len(status['missing'])} / {total}\n")

    for entry in HS_FIELD_SCHEMA:
        name = entry[0]
        marker = "✓" if name in status["existing"] else "·"
        print(f"  {marker} {name}")
    print()

    # Surface potential conflicts on first run
    if status["missing"]:
        conflicts = find_potential_conflicts()
        if conflicts:
            print("⚠️  POTENTIAL CONFLICTS — existing HubSpot properties with overlapping names:")
            for c in conflicts:
                print(f"    {c['name']:<40}  label='{c['label']}'  type={c['type']}")
            print("    Review these in HubSpot before pushing. If any of these are the")
            print("    canonical field (e.g. MMC's check-in date), update HS_FIELDS in")
            print("    config.py to point at them instead of creating duplicates.\n")

    if check_only:
        return 0
    if not status["missing"]:
        print("All fields already exist. Nothing to do.")
        return 0
    if not push:
        print(f"DRY RUN — would create {len(status['missing'])} fields.")
        print("Re-run with --push to actually create them.")
        return 0

    # Create missing fields
    print(f"Creating {len(status['missing'])} fields in HubSpot...\n")
    created = 0
    for entry in HS_FIELD_SCHEMA:
        name, label, type_, field_type, options = entry
        if name in status["existing"]:
            continue
        try:
            create_company_property(name, label, type_, field_type, options)
            print(f"  ✓ created {name}")
            created += 1
        except Exception as e:
            print(f"  ✗ FAILED {name}: {e}")
    print(f"\nDone. {created}/{len(status['missing'])} fields created.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Create HubSpot custom fields for route builder")
    p.add_argument("--push", action="store_true", help="Actually create missing fields (default: dry-run)")
    p.add_argument("--check", action="store_true", help="Report status only (no conflict scan)")
    args = p.parse_args()
    sys.exit(main(push=args.push, check_only=args.check))
