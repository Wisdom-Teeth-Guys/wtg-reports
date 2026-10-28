#!/usr/bin/env python3
"""Rebuild marketer_reports/funnel_dashboard_data.json for the Patient Funnel
Dashboard (deal creation -> insurance obtained -> verified -> scheduled -> won).

Pulls deal-level funnel fields directly from HubSpot (not the Google Sheet --
insurance_type/deal_outcome/appt_date/lead source aren't synced there) and
aggregates into (month, market, insurance_type, lead_source) buckets. No PHI,
no deal names/ids in the output -- counts only.

Requires HUBSPOT_TOKEN in the environment.
"""
import json
import os
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "out" / "marketer_reports"
OUT_FILE = OUT_DIR / "funnel_dashboard_data.json"

HS_BASE = "https://api.hubapi.com"
TOKEN = os.environ["HUBSPOT_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

PATIENT_PIPELINES = {
    "2161131227": "Austin",
    "2161217265": "Dallas",
    "2161131224": "Houston",
    "2161131216": "Phoenix",
    "2161131215": "San Antonio",
    "2161131221": "Tucson",
    "2161217271": "Utah",
}

PROPS = [
    "createdate", "pipeline", "insurance_type", "num_contacted_notes",
    "medicaid_last_verified_date", "verification_last_triggered",
    "appt_date", "deal_outcome", "migrated_primary_lead_source",
]

MIN_CREATEDATE = "2022-01-01T00:00:00Z"

# Canonical lead-source buckets. The raw field has ~60 values (mostly
# near-zero radio stations, deprecated "Z - ..." options, etc.) -- bucketing
# to the ones with real volume keeps the filter usable. Everything else
# (including blank) folds into Other/Unassigned.
LEAD_SOURCE_BUCKETS = {
    "Dentist Referral": "Dentist Referral",
    "Orthodontist Referral": "Orthodontist Referral",
    "Friend or Family Referral": "Friend or Family Referral",
    "Insurance Company Referral": "Insurance Company Referral",
    "Google Adwords PPC": "Google Adwords PPC",
    "Google Organic": "Google Organic",
    "Have Not Made Contact Yet": "Have Not Made Contact Yet",
    "Forgot to Ask - Will Try to Get": "Forgot to Ask - Will Try to Get",
}


def bucket_lead_source(raw):
    if not raw:
        return "Unassigned"
    return LEAD_SOURCE_BUCKETS.get(raw, "Other")


def hs_post(path, body, attempts=5):
    for i in range(attempts):
        r = requests.post(f"{HS_BASE}{path}", headers=HEADERS, json=body, timeout=30)
        if r.status_code == 429 or 500 <= r.status_code < 600:
            time.sleep(2 ** i)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def fetch_page(last_id):
    filters = [
        {"propertyName": "pipeline", "operator": "IN", "values": list(PATIENT_PIPELINES.keys())},
        {"propertyName": "createdate", "operator": "GTE", "value": MIN_CREATEDATE},
    ]
    if last_id:
        filters.append({"propertyName": "hs_object_id", "operator": "GT", "value": str(last_id)})
    body = {
        "limit": 100,
        "properties": PROPS,
        "filterGroups": [{"filters": filters}],
        "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
    }
    return hs_post("/crm/v3/objects/deals/search", body)


def main():
    last_id = None
    page_num = 0
    total_seen = 0
    buckets = {}  # (month, market, insurance_type, lead_source) -> counters

    while True:
        page_num += 1
        result = fetch_page(last_id)
        deals = result.get("results", [])
        if not deals:
            break

        for d in deals:
            p = d.get("properties", {})
            total_seen += 1

            createdate = p.get("createdate") or ""
            month = createdate[:7] if len(createdate) >= 7 else "unknown"
            market = PATIENT_PIPELINES.get(p.get("pipeline"), "Unknown")
            ins_type = p.get("insurance_type") or "Unassigned"
            lead_source = bucket_lead_source(p.get("migrated_primary_lead_source"))

            key = (month, market, ins_type, lead_source)
            if key not in buckets:
                buckets[key] = {
                    "created": 0, "contacted": 0, "medicaid_verified": 0,
                    "private_verif_triggered": 0, "scheduled": 0, "won": 0, "lost": 0,
                }
            b = buckets[key]
            b["created"] += 1

            try:
                num_contacted = float(p.get("num_contacted_notes") or 0)
            except ValueError:
                num_contacted = 0
            if num_contacted > 0:
                b["contacted"] += 1

            if ins_type in ("Medicaid", "CHIP") and p.get("medicaid_last_verified_date"):
                b["medicaid_verified"] += 1

            if ins_type in ("HMO", "PPO") and p.get("verification_last_triggered"):
                b["private_verif_triggered"] += 1

            if p.get("appt_date"):
                b["scheduled"] += 1

            outcome = p.get("deal_outcome")
            if outcome == "Won":
                b["won"] += 1
            elif outcome == "Lost":
                b["lost"] += 1

        print(f"  page {page_num}: seen={len(deals)} running-total={total_seen}")

        last_id = max(int(d["id"]) for d in deals)
        if len(deals) < 100:
            break

    rows = []
    for (month, market, ins_type, lead_source), counts in sorted(buckets.items()):
        row = {"month": month, "market": market, "insurance_type": ins_type, "lead_source": lead_source}
        row.update(counts)
        rows.append(row)

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "min_createdate": MIN_CREATEDATE,
        "rows": rows,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, separators=(",", ":")))

    print()
    print(f"Done. total deals: {total_seen}, buckets: {len(rows)}")
    print(f"wrote {OUT_FILE} ({OUT_FILE.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
