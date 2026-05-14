#!/usr/bin/env python3
"""
Pull all deals + companies from HubSpot and write to a Google Sheet.
Runs weekly via GitHub Actions.

Required env vars (set as GitHub Secrets):
  HUBSPOT_TOKEN        — HubSpot Private App access token
  GOOGLE_SHEET_ID      — the ID portion of the Google Sheet URL
  GOOGLE_SA_JSON       — full JSON of the Google Service Account key (as a string)
"""

import os
import json
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
import gspread
from google.oauth2.service_account import Credentials


HUBSPOT_TOKEN   = os.environ["HUBSPOT_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SA_JSON  = os.environ["GOOGLE_SA_JSON"]

# Only pull deals created on or after this date. Reports cover 2024+ only.
# Override via env var if you ever need a different start.
DEAL_START_DATE = os.environ.get("DEAL_START_DATE", "2024-01-01")

HS_BASE = "https://api.hubapi.com"
HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type": "application/json",
}

# Deal properties to pull. Update these names if your HubSpot uses different
# internal property names — find them under Settings → Properties.
DEAL_PROPERTIES = [
    "createdate",
    "closedate",
    "dealstage",
    "pipeline",
    # Custom + migrated properties (verified populated in HubSpot)
    "market",                                       # custom "Territory" property
    "closed_won_time",                              # custom won time
    # TODO June 2026: Once HubSpot workflows populate the non-migrated versions,
    # switch these back to the clean property names (marketer_assigned,
    # zip_code, general_dentist). See GitHub issue.
    "migrated_marketer_assigned__dentist_referral",
    "migrated_zip_code",
    "migrated_general_dentist__city__phone_number",
]

COMPANY_PROPERTIES = [
    "name",
    "zip",
    "market",     # broad market: "Dallas", "Utah"
    "market2",    # fine territory: "Dallas SW", "Utah South"
]


# ─────────────────────────────────────────────────────────────────────────────
# HubSpot fetchers
# ─────────────────────────────────────────────────────────────────────────────

def _request_with_retry(method: str, url: str, **kwargs):
    """HTTP request with retry on 429 / 5xx errors (exponential backoff)."""
    for attempt in range(6):
        try:
            r = requests.request(method, url, timeout=60, **kwargs)
        except requests.exceptions.RequestException as e:
            wait = 2 ** attempt
            print(f"    ! network error ({e}); retrying in {wait}s", flush=True)
            time.sleep(wait)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            wait = 2 ** attempt
            print(f"    ! HTTP {r.status_code}; retrying in {wait}s", flush=True)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    # Final attempt — let it raise
    r = requests.request(method, url, timeout=60, **kwargs)
    r.raise_for_status()
    return r


def hs_get_all(endpoint: str, properties: list, associations: list = None):
    """Generic paginator for HubSpot v3 objects (deals, companies, etc)."""
    url = f"{HS_BASE}{endpoint}"
    after = None
    results = []
    page = 0
    while True:
        params = {
            "limit": 100,
            "properties": ",".join(properties),
        }
        if associations:
            params["associations"] = ",".join(associations)
        if after:
            params["after"] = after

        r = _request_with_retry("GET", url, headers=HEADERS, params=params)
        body = r.json()
        results.extend(body.get("results", []))
        page += 1
        if page % 10 == 0:
            print(f"    … {len(results)} records so far", flush=True)

        paging = body.get("paging", {}).get("next")
        if not paging:
            break
        after = paging.get("after")
    return results


def _search_deals_window(properties: list, start_ms: int, end_ms: int):
    """Search deals created within a [start_ms, end_ms) window. Max 10K results per HubSpot rules."""
    url = f"{HS_BASE}/crm/v3/objects/deals/search"
    after = None
    out = []
    while True:
        payload = {
            "filterGroups": [{
                "filters": [
                    {"propertyName": "createdate", "operator": "GTE", "value": str(start_ms)},
                    {"propertyName": "createdate", "operator": "LT",  "value": str(end_ms)},
                ],
            }],
            "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}],
            "properties": properties,
            "limit": 100,
        }
        if after:
            payload["after"] = after
        r = _request_with_retry("POST", url, headers=HEADERS, json=payload)
        body = r.json()
        out.extend(body.get("results", []))
        paging = body.get("paging", {}).get("next")
        if not paging:
            break
        after = paging.get("after")
        # Hard safety: if a single window somehow exceeds 10K, stop and warn
        if len(out) >= 10000:
            print(f"    ! window {start_ms}-{end_ms} hit 10K cap; some deals may be missed", flush=True)
            break
    return out


def hs_search_deals(properties: list, associations: list, since_iso: str):
    """Pull deals in monthly chunks to stay under HubSpot's 10K-per-query cap."""
    since = datetime.strptime(since_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    # Build month-aligned windows from `since` → `now`
    windows = []
    cur = since.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur < now:
        # next month
        if cur.month == 12:
            nxt = cur.replace(year=cur.year + 1, month=1)
        else:
            nxt = cur.replace(month=cur.month + 1)
        windows.append((cur, min(nxt, now)))
        cur = nxt

    results = []
    for i, (start, end) in enumerate(windows, 1):
        start_ms = int(start.timestamp() * 1000)
        end_ms   = int(end.timestamp()   * 1000)
        batch = _search_deals_window(properties, start_ms, end_ms)
        results.extend(batch)
        print(f"    … window {i}/{len(windows)} ({start:%Y-%m}): +{len(batch)} deals (total {len(results)})", flush=True)

    if associations and results:
        print(f"    Fetching company associations for {len(results)} deals…", flush=True)
        _attach_company_associations(results)
    return results


def _attach_company_associations(deals: list):
    """Batch-fetch company associations for the given deals."""
    url = f"{HS_BASE}/crm/v4/associations/deals/companies/batch/read"
    deal_ids = [d["id"] for d in deals]
    assoc_map = {}
    for i in range(0, len(deal_ids), 100):
        chunk = deal_ids[i:i+100]
        payload = {"inputs": [{"id": did} for did in chunk]}
        r = _request_with_retry("POST", url, headers=HEADERS, json=payload)
        body = r.json()
        for result in body.get("results", []):
            from_id = result.get("from", {}).get("id")
            to = result.get("to", [])
            if from_id and to:
                assoc_map[from_id] = to[0].get("toObjectId")
    for d in deals:
        cid = assoc_map.get(d["id"])
        if cid:
            d.setdefault("associations", {}).setdefault("companies", {})["results"] = [{"id": str(cid)}]


def fetch_owners():
    """Get all HubSpot owners (users) so we can resolve hubspot_owner_id → name/email."""
    url = f"{HS_BASE}/crm/v3/owners"
    after = None
    out = {}
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        r = requests.get(url, headers=HEADERS, params=params, timeout=60)
        r.raise_for_status()
        body = r.json()
        for o in body.get("results", []):
            out[str(o["id"])] = {
                "email":      o.get("email", ""),
                "first_name": o.get("firstName", ""),
                "last_name":  o.get("lastName", ""),
            }
        paging = body.get("paging", {}).get("next")
        if not paging:
            break
        after = paging.get("after")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Flatteners
# ─────────────────────────────────────────────────────────────────────────────

def flatten_deals(deals: list, companies_by_id: dict, owners: dict):
    """Flatten deal objects into rows. Columns match Pipedrive export structure."""
    rows = []
    for d in deals:
        props = d.get("properties", {})
        assoc = d.get("associations", {}).get("companies", {}).get("results", [])
        company_id = assoc[0]["id"] if assoc else None
        company = companies_by_id.get(company_id, {}) if company_id else {}

        rows.append({
            "deal_id":           d.get("id"),
            "pipeline":          props.get("pipeline", ""),
            "deal_stage":        props.get("dealstage", ""),
            "create_date":       props.get("createdate", ""),
            "close_date":        props.get("closedate", ""),
            "won_time":          props.get("closed_won_time", ""),
            "territory":         props.get("market", ""),
            "marketer_assigned": props.get("migrated_marketer_assigned__dentist_referral", ""),
            "general_dentist":   props.get("migrated_general_dentist__city__phone_number", ""),
            "deal_zip":          props.get("migrated_zip_code", ""),
            "company_id":         company_id or "",
            "company_name":       company.get("name", ""),
            "company_zip":        company.get("zip", ""),
            "company_market":     company.get("market", ""),
            "company_territory":  company.get("market2", ""),
        })
    return rows


def companies_to_rows(companies: list):
    out = []
    for c in companies:
        p = c.get("properties", {})
        out.append({
            "company_id": c.get("id"),
            "name":       p.get("name", ""),
            "zip":        p.get("zip", ""),
            "market":     p.get("market", ""),
            "territory":  p.get("market2", ""),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets writer
# ─────────────────────────────────────────────────────────────────────────────

def write_tab(sheet, tab_name: str, rows: list):
    """Overwrite a tab with the given rows (list of dicts). Creates the tab if missing."""
    if not rows:
        print(f"  ! no rows for {tab_name}, skipping")
        return

    headers = list(rows[0].keys())
    data = [headers] + [[r.get(h, "") for h in headers] for r in rows]

    try:
        ws = sheet.worksheet(tab_name)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=tab_name, rows=len(data) + 10, cols=len(headers))

    ws.update(data, value_input_option="RAW")
    print(f"  ✓ wrote {len(rows)} rows to '{tab_name}'")


def get_sheet():
    creds_dict = json.loads(GOOGLE_SA_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"],
    )
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Owners no longer needed — Marketer Assigned is a custom property
    owners = {}

    print("Fetching HubSpot companies…")
    companies = hs_get_all("/crm/v3/objects/companies", COMPANY_PROPERTIES)
    companies_by_id = {c["id"]: c.get("properties", {}) for c in companies}
    print(f"  → {len(companies)} companies")

    since_iso = f"{DEAL_START_DATE}T00:00:00Z"
    print(f"Fetching HubSpot deals (created since {since_iso})…")
    deals = hs_search_deals(
        DEAL_PROPERTIES,
        associations=["companies"],
        since_iso=since_iso,
    )
    print(f"  → {len(deals)} deals")

    print("Flattening…")
    deal_rows    = flatten_deals(deals, companies_by_id, owners)
    company_rows = companies_to_rows(companies)
    meta_rows = [{
        "key": "last_synced_utc",
        "value": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, {
        "key": "deal_count",
        "value": str(len(deal_rows)),
    }, {
        "key": "company_count",
        "value": str(len(company_rows)),
    }]

    print("Writing to Google Sheet…")
    sheet = get_sheet()
    write_tab(sheet, "deals_raw",     deal_rows)
    write_tab(sheet, "companies_raw", company_rows)
    write_tab(sheet, "_meta",         meta_rows)

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTP error: {e.response.status_code} {e.response.text}", file=sys.stderr)
        sys.exit(1)
