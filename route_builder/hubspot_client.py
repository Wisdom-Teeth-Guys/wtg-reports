"""
HubSpot CRM v3 API client for the route builder.

Pattern matches the existing `territory_zip_export.py`:
    - stdlib urllib only (no requests dependency)
    - Token loaded manually from .env
    - hs_get / hs_post / hs_patch helpers
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator, Optional

from .config import HS_BASE_URL, load_env


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
_HS_TOKEN_CACHE = {"token": None}


def get_token() -> str:
    if _HS_TOKEN_CACHE["token"]:
        return _HS_TOKEN_CACHE["token"]
    env = load_env()
    tok = env.get("HUBSPOT_TOKEN")
    if not tok:
        raise RuntimeError("Missing HUBSPOT_TOKEN in .env")
    _HS_TOKEN_CACHE["token"] = tok
    return tok


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------
def _request(method: str, path: str, body: Optional[dict] = None, timeout: int = 30):
    url = f"{HS_BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            if not payload:
                return None
            return json.loads(payload)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:1000] if e.fp else ""
        raise RuntimeError(f"HubSpot {e.code} on {method} {path}: {body_text}") from e


def hs_get(path: str):
    return _request("GET", path)


def hs_post(path: str, body: dict):
    return _request("POST", path, body=body)


def hs_patch(path: str, body: dict):
    return _request("PATCH", path, body=body)


# ---------------------------------------------------------------------------
# Properties (custom fields)
# ---------------------------------------------------------------------------
def fetch_company_properties() -> list[dict]:
    """List of all properties on the Company object."""
    return hs_get("/crm/v3/properties/companies?limit=1000")["results"]


def check_custom_fields(expected: list[tuple]) -> dict:
    """Given the `HS_FIELD_SCHEMA` list, return which fields exist and which don't.

    Returns dict: {"existing": [internal_names], "missing": [internal_names]}
    """
    existing_names = {p["name"] for p in fetch_company_properties()}
    existing = []
    missing = []
    for entry in expected:
        name = entry[0]
        (existing if name in existing_names else missing).append(name)
    return {"existing": existing, "missing": missing}


def create_company_property(
    name: str,
    label: str,
    type_: str,
    field_type: str,
    options: Optional[list] = None,
    group_name: str = "companyinformation",
) -> dict:
    """Create a single custom property on the Company object."""
    body = {
        "name": name,
        "label": label,
        "type": type_,
        "fieldType": field_type,
        "groupName": group_name,
    }
    if field_type == "booleancheckbox":
        # HubSpot requires booleans to declare both options explicitly.
        body["options"] = [
            {"label": "True",  "value": "true",  "displayOrder": 0},
            {"label": "False", "value": "false", "displayOrder": 1},
        ]
    elif options:
        body["options"] = [
            {"label": _humanize(o), "value": o, "displayOrder": i}
            for i, o in enumerate(options)
        ]
    return hs_post("/crm/v3/properties/companies", body)


def _humanize(slug: str) -> str:
    """morning → Morning,  contacted_dm → Contacted Dm.  For dropdown labels."""
    return slug.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------
def iter_companies(properties: list[str], page_size: int = 100) -> Iterator[dict]:
    """Yield every company. Uses the List endpoint (no 10k search cap)."""
    after = None
    while True:
        qs = {"limit": page_size}
        if after:
            qs["after"] = after
        # HubSpot wants `properties=name&properties=zip&...` (repeated keys).
        prop_qs = "&".join(f"properties={urllib.parse.quote(p)}" for p in properties)
        rest = urllib.parse.urlencode(qs)
        path = f"/crm/v3/objects/companies?{rest}&{prop_qs}"
        data = hs_get(path)
        for item in data.get("results", []):
            yield item
        paging = data.get("paging") or {}
        after = paging.get("next", {}).get("after")
        if not after:
            return


def iter_deals(properties: list[str], page_size: int = 100,
                with_company_assoc: bool = True) -> Iterator[dict]:
    """Yield every deal. Uses the List endpoint (no 10k search cap).

    If `with_company_assoc=True`, includes `associations.companies` in each
    record so callers can group deals by company without a second API round-trip.
    """
    after = None
    while True:
        qs = {"limit": page_size}
        if after:
            qs["after"] = after
        if with_company_assoc:
            qs["associations"] = "companies"
        prop_qs = "&".join(f"properties={urllib.parse.quote(p)}" for p in properties)
        rest = urllib.parse.urlencode(qs)
        path = f"/crm/v3/objects/deals?{rest}&{prop_qs}"
        data = hs_get(path)
        for item in data.get("results", []):
            yield item
        paging = data.get("paging") or {}
        after = paging.get("next", {}).get("after")
        if not after:
            return


def search_companies(filter_groups: list, properties: list[str], limit: int = 100) -> list[dict]:
    """POST /crm/v3/objects/companies/search — supports more advanced filters.

    Example filter_groups:
        [{"filters": [{"propertyName": "visit_week_of",
                       "operator": "EQ",
                       "value": "2026-05-11"}]}]
    """
    body = {
        "filterGroups": filter_groups,
        "properties": properties,
        "limit": min(limit, 100),
    }
    results = []
    after = None
    while True:
        if after:
            body["after"] = after
        data = hs_post("/crm/v3/objects/companies/search", body)
        results.extend(data.get("results", []))
        paging = data.get("paging") or {}
        after = paging.get("next", {}).get("after")
        if not after or len(results) >= 10_000:
            return results


def batch_update_companies(updates: list[dict], chunk_size: int = 100,
                            sleep_between: float = 0.1) -> int:
    """POST /crm/v3/objects/companies/batch/update in chunks of 100 (HubSpot's max).

    `updates` items must look like: {"id": "<hs_id>", "properties": {...}}

    Returns the number of successfully-sent updates.
    """
    sent = 0
    for i in range(0, len(updates), chunk_size):
        chunk = updates[i:i + chunk_size]
        payload = {"inputs": chunk}
        hs_post("/crm/v3/objects/companies/batch/update", payload)
        sent += len(chunk)
        if sleep_between and i + chunk_size < len(updates):
            time.sleep(sleep_between)
    return sent


# ---------------------------------------------------------------------------
# Quick CLI for verification
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from .config import HS_FIELD_SCHEMA
    tok = get_token()
    print(f"HubSpot token loaded ({len(tok)} chars)")

    status = check_custom_fields(HS_FIELD_SCHEMA)
    print(f"Custom fields — existing: {len(status['existing'])}, missing: {len(status['missing'])}")
    if status["missing"]:
        print("  Missing:")
        for m in status["missing"]:
            print(f"    - {m}")
    else:
        print("  All 15 fields exist.")
