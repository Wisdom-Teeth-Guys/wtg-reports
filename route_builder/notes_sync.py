"""Sync historical SPOTIO Visit notes → HubSpot Notes (which then sync to MMC).

Flow:
  1. Index every HubSpot company by address signature.
  2. Walk SPOTIO leads. Match each to a HubSpot company.
  3. For each matched lead, fetch Visit activities; keep ones with non-empty notes.
  4. For each note, POST to /crm/v3/objects/notes with company association.
     The note body is prefixed with [Spotio Visit · YYYY-MM-DD · Result · sp_id <visit_id>]
     so re-runs can detect duplicates by searching that header.

HubSpot↔MMC sync carries the new notes down to MMC automatically (Sync Notes on
Companies is enabled on the integration).

Dry-run by default. Use --push to actually create notes.

Usage:
    python3 -m route_builder.notes_sync --limit 5            # dry-run on first 5 matched leads
    python3 -m route_builder.notes_sync --limit 5 --push     # actually create
    python3 -m route_builder.notes_sync --push               # full backfill (long)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import address_matcher, config, hubspot_client, spotio_client

ROOT = Path(__file__).parent.parent
HS_BASE = "https://api.hubapi.com"
HS_NOTE_TO_COMPANY_TYPE_ID = 190  # association typeId: note → company (default)

# Build SPOTIO result_id → human label from the existing config
SPOTIO_OUTCOME_LABELS = {
    rid: outcome.replace("_", " ").title()
    for rid, outcome in config.SPOTIO_RESULT_TO_OUTCOME.items()
}


def load_env() -> dict:
    out = {}
    env_path = ROOT / ".env"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def hs_owners(token: str) -> dict[str, str]:
    """Return {email_lower: hs_owner_id}."""
    out = {}
    after = None
    while True:
        params = {"limit": 100}
        if after: params["after"] = after
        url = f"{HS_BASE}/crm/v3/owners/?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        d = json.load(urllib.request.urlopen(req, timeout=30))
        for o in d.get("results", []):
            email = (o.get("email") or "").lower()
            if email:
                out[email] = o["id"]
        after = (d.get("paging") or {}).get("next", {}).get("after")
        if not after: break
    return out


def spotio_user_emails() -> dict[str, str]:
    """Return {spotio_user_id: email_lower}."""
    users = spotio_client.fetch_users()
    return {str(u["id"]): (u.get("email") or "").lower() for u in users if u.get("email")}


def format_note_body(visit: dict, lead: dict) -> str:
    raw = (visit.get("notes") or "").strip()
    date = (visit.get("date") or "")[:10]
    result_id = str(visit.get("resultId") or "")
    label = SPOTIO_OUTCOME_LABELS.get(result_id, f"Result {result_id}")
    visit_id = visit.get("id")
    header = f"[Spotio Visit · {date} · {label} · sp_id:{visit_id}]"
    return f"{header}\n\n{raw}"


def create_hs_note(token: str, hs_company_id: str, owner_id: str | None,
                   body: str, timestamp_iso: str) -> dict:
    """POST a new note + associate it to the company in a single request."""
    props = {"hs_note_body": body, "hs_timestamp": timestamp_iso}
    if owner_id:
        props["hubspot_owner_id"] = owner_id
    payload = {
        "properties": props,
        "associations": [{
            "to": {"id": str(hs_company_id)},
            "types": [{"associationCategory": "HUBSPOT_DEFINED",
                       "associationTypeId": HS_NOTE_TO_COMPANY_TYPE_ID}],
        }],
    }
    req = urllib.request.Request(
        f"{HS_BASE}/crm/v3/objects/notes",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    return json.load(urllib.request.urlopen(req, timeout=30))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after N matched leads processed (default: all).")
    p.add_argument("--push", action="store_true",
                   help="Actually create HubSpot notes. Default is dry-run.")
    p.add_argument("--since", default=None,
                   help="Only sync visits whose date is >= this ISO date (e.g. 2024-01-01).")
    p.add_argument("--out", default="notes_sync_log.json",
                   help="Output JSON log path (under cwd).")
    args = p.parse_args()

    env = load_env()
    if "HUBSPOT_TOKEN" not in env:
        print("ERROR: HUBSPOT_TOKEN not in .env", file=sys.stderr); return 2
    hs_token = env["HUBSPOT_TOKEN"]

    print("[1/5] Fetching HubSpot owners (for Spotio user → HS owner mapping)...", file=sys.stderr)
    owners_by_email = hs_owners(hs_token)
    print(f"      {len(owners_by_email)} owners", file=sys.stderr)

    print("[2/5] Fetching Spotio users...", file=sys.stderr)
    sp_user_emails = spotio_user_emails()
    sp_user_to_hs_owner = {
        sp_id: owners_by_email.get(email)
        for sp_id, email in sp_user_emails.items()
    }
    mapped = sum(1 for v in sp_user_to_hs_owner.values() if v)
    print(f"      {mapped}/{len(sp_user_to_hs_owner)} Spotio users mapped to HS owners by email",
          file=sys.stderr)

    print("[3/5] Indexing HubSpot companies by address signature...", file=sys.stderr)
    t0 = time.time()
    hs_companies = list(hubspot_client.iter_companies(["name", "address", "city", "state", "zip"]))
    hs_index = address_matcher.build_hs_index(hs_companies)
    print(f"      {len(hs_companies):,} companies, {len(hs_index):,} signatures, {time.time()-t0:.1f}s",
          file=sys.stderr)

    print("[4/5] Walking Spotio leads and matching...", file=sys.stderr)
    matched_leads = []
    scanned = 0
    for lead in spotio_client.iter_leads(page_size=100):
        scanned += 1
        if scanned % 1000 == 0:
            print(f"      scanned {scanned:,} (matched {len(matched_leads):,})", file=sys.stderr)
        if (lead.get("visitsCount", 0) or 0) == 0:
            continue
        status, hs = address_matcher.find_match(lead, hs_index)
        if status == "unique":
            matched_leads.append((hs, lead))
        if args.limit is not None and len(matched_leads) >= args.limit:
            break
    print(f"      scanned={scanned:,} matched={len(matched_leads):,}", file=sys.stderr)

    print(f"[5/5] Fetching activities and creating notes ({'PUSH' if args.push else 'DRY-RUN'})...",
          file=sys.stderr)
    log = []
    n_visits = n_notes = n_skipped_empty = n_skipped_too_old = n_failed = 0
    t1 = time.time()
    for i, (hs_co, lead) in enumerate(matched_leads):
        hs_id = hs_co["id"]
        try:
            acts = spotio_client.fetch_lead_activities(lead["id"])
        except Exception as e:
            print(f"      WARN failed to fetch activities for lead {lead['id']}: {e}", file=sys.stderr)
            continue
        visits = [a for a in acts if a.get("activityTemplateId") == config.SPOTIO_VISIT_TEMPLATE_ID]
        n_visits += len(visits)
        for v in visits:
            note_text = (v.get("notes") or "").strip()
            if not note_text:
                n_skipped_empty += 1
                continue
            visit_date = v.get("date") or ""
            if args.since and visit_date[:10] < args.since:
                n_skipped_too_old += 1
                continue
            owner_id = sp_user_to_hs_owner.get(str(v.get("ownerId") or ""))
            body = format_note_body(v, lead)
            entry = {
                "spotio_visit_id": v.get("id"),
                "spotio_lead_id": lead["id"],
                "hs_company_id": hs_id,
                "hs_company_name": (hs_co.get("properties") or {}).get("name"),
                "spotio_owner_id": v.get("ownerId"),
                "hs_owner_id": owner_id,
                "visit_date": visit_date,
                "body_preview": body[:160],
            }
            if not args.push:
                entry["status"] = "dry_run"
                log.append(entry)
                n_notes += 1
                continue
            try:
                resp = create_hs_note(hs_token, hs_id, owner_id, body, visit_date)
                entry["status"] = "ok"
                entry["hs_note_id"] = resp.get("id")
                n_notes += 1
            except urllib.error.HTTPError as e:
                entry["status"] = "fail"
                entry["error"] = f"HTTP {e.code}: {e.read().decode()[:200]}"
                n_failed += 1
            except Exception as e:
                entry["status"] = "fail"
                entry["error"] = f"{type(e).__name__}: {e}"
                n_failed += 1
            log.append(entry)
        if (i + 1) % 20 == 0:
            elapsed = time.time() - t1
            rate = (i + 1) / elapsed if elapsed else 0
            remaining = (len(matched_leads) - i - 1) / rate if rate else 0
            print(f"      {i+1}/{len(matched_leads)} leads | notes={n_notes} | "
                  f"{rate:.1f}/s | ~{remaining/60:.1f}min", file=sys.stderr)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(log, indent=2))
    print(f"\nSummary:", file=sys.stderr)
    print(f"  matched leads:         {len(matched_leads):,}", file=sys.stderr)
    print(f"  total visit activities: {n_visits:,}", file=sys.stderr)
    print(f"  visits with notes:     {n_notes:,}", file=sys.stderr)
    print(f"  skipped empty:         {n_skipped_empty:,}", file=sys.stderr)
    print(f"  skipped too old:       {n_skipped_too_old:,}", file=sys.stderr)
    print(f"  failed (push only):    {n_failed:,}", file=sys.stderr)
    print(f"  log:                   {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
