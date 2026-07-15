"""
SPOTIO API client.

Auth flow (from developer.spotio2.com docs):
    POST /api/users/apitoken with {clientId, secret} → JSON {accessToken: "<JWT>"}
    Use the JWT as Bearer token on subsequent requests.

Endpoints used:
    GET  /api/users                               — all SPOTIO users (reps)
    GET  /api/territories                         — territory list
    GET  /api/leads?limit=N&sort=lastActivityTime&order=desc[&scrollId=...]
                                                  — paginated leads, sortable
    GET  /api/leads/{leadId}                      — single lead detail
    GET  /api/leads/{leadId}/activities           — all activities (visits etc.)
    GET  /api/activityTemplates                   — template defs (id=1 is "Visit")
    GET  /api/activityResults                     — outcome dropdown list

Pagination: list endpoints return `{scrollId, totalCount, items: [...]}`.
            Continue with `?scrollId=<token>` until items is empty.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator, Optional

from .config import (
    SPOTIO_BASE_URL,
    SPOTIO_TOKEN_ENDPOINT,
    SPOTIO_USER_AGENT,
    SPOTIO_VISIT_TEMPLATE_ID,
    load_env,
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
_TOKEN_CACHE = {"token": None}


def get_token(force_refresh: bool = False) -> str:
    """Exchange Client ID + Secret for a Bearer access token.

    Caches the token in-process for the lifetime of the script run.
    """
    if _TOKEN_CACHE["token"] and not force_refresh:
        return _TOKEN_CACHE["token"]

    env = load_env()
    client_id = env.get("SPOTIO_CLIENT_ID")
    secret = env.get("SPOTIO_API_SECRET")
    if not client_id or not secret:
        raise RuntimeError("Missing SPOTIO_CLIENT_ID or SPOTIO_API_SECRET in .env")

    body = json.dumps({"clientId": client_id, "secret": secret}).encode()
    req = urllib.request.Request(
        f"{SPOTIO_BASE_URL}{SPOTIO_TOKEN_ENDPOINT}",
        data=body,
        headers={
            "Accept": "text/plain",
            "Content-Type": "application/merge-patch+json",
            "User-Agent": SPOTIO_USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    token = payload.get("accessToken")
    if not token:
        raise RuntimeError(f"Unexpected token response: {payload}")
    _TOKEN_CACHE["token"] = token
    return token


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {get_token()}",
        "Accept": "application/json",
        "User-Agent": SPOTIO_USER_AGENT,
    }


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------
def _get(path: str, params: Optional[dict] = None, timeout: int = 30):
    """GET <SPOTIO_BASE_URL><path> with auth. Returns parsed JSON."""
    url = f"{SPOTIO_BASE_URL}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
    req = urllib.request.Request(url, headers=_auth_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500] if e.fp else ""
        raise RuntimeError(f"SPOTIO {e.code} on GET {path}: {body}") from e


# ---------------------------------------------------------------------------
# Reference data (templates, results, users, territories)
# ---------------------------------------------------------------------------
def fetch_users() -> list[dict]:
    """All SPOTIO users (reps)."""
    return _get("/api/users")


def fetch_territories() -> list[dict]:
    """All territories with `userIds` assignment lists."""
    return _get("/api/territories")


def fetch_activity_templates() -> list[dict]:
    """Activity templates. id=1 is "Visit" (verify with config.SPOTIO_VISIT_TEMPLATE_ID)."""
    return _get("/api/activityTemplates")


def fetch_activity_results() -> list[dict]:
    """All activity result definitions. Filter by `activityTemplateId == "1"` for visit outcomes."""
    return _get("/api/activityResults")


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------
def iter_leads(
    page_size: int = 100,
    sort: str = "lastActivityTime",
    order: str = "desc",
    max_results: int = 10000,
) -> Iterator[dict]:
    """Yield leads page by page, sorted by `sort` desc by default.

    SPOTIO's /api/leads endpoint enforces `from + size <= 10000`, so we cannot
    retrieve more than the 10,000 most-recently-active leads in one walk. We cap
    at `max_results` and stop gracefully. Because leads are sorted by
    lastActivityTime desc, the retrievable set is exactly the most-recently-active
    offices — which is what matters for visit-recency.

    Caller can also stop early once `lastActivityTime` falls outside their window.
    """
    scroll_id = None
    yielded = 0
    while True:
        # Keep `from + size` strictly under the 10k ceiling.
        remaining = max_results - yielded
        if remaining <= 0:
            return
        size = min(page_size, remaining)
        params = {"limit": size, "sort": sort, "order": order}
        if scroll_id:
            params["scrollId"] = scroll_id
        data = _get("/api/leads", params=params)
        items = data.get("items", [])
        if not items:
            return
        for item in items:
            yield item
            yielded += 1
        # Advance pagination
        next_scroll = data.get("scrollId")
        if not next_scroll or next_scroll == scroll_id:
            return
        scroll_id = next_scroll


def fetch_lead(lead_id: str) -> dict:
    """Single lead detail."""
    return _get(f"/api/leads/{lead_id}")


def fetch_lead_activities(lead_id: str) -> list[dict]:
    """All activities for a lead (visits, owner changes, stage transitions, etc.).

    Filter callers should keep only:
        a["type"] == "event"
        AND a.get("activityTemplateId") == SPOTIO_VISIT_TEMPLATE_ID
        AND a.get("done") is True
    """
    return _get(f"/api/leads/{lead_id}/activities")


def fetch_visit_activities(lead_id: str) -> list[dict]:
    """Convenience: only completed visit-template activities for a lead."""
    all_acts = fetch_lead_activities(lead_id)
    return [
        a for a in all_acts
        if a.get("type") == "event"
        and a.get("activityTemplateId") == SPOTIO_VISIT_TEMPLATE_ID
        and a.get("done")
    ]


# ---------------------------------------------------------------------------
# Quick CLI for verification
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Testing SPOTIO API connection...")
    tok = get_token()
    print(f"  ✓ Token obtained ({len(tok)} chars)")

    users = fetch_users()
    active = [u for u in users if u.get("status") == "active"]
    print(f"  ✓ Users: {len(users)} total, {len(active)} active")

    terrs = fetch_territories()
    print(f"  ✓ Territories: {len(terrs)}")

    templates = fetch_activity_templates()
    visit = next((t for t in templates if t["id"] == SPOTIO_VISIT_TEMPLATE_ID), None)
    print(f"  ✓ Activity templates: {len(templates)} (Visit template: {visit['title'] if visit else 'NOT FOUND'})")

    results = fetch_activity_results()
    visit_results = [r for r in results if r.get("activityTemplateId") == SPOTIO_VISIT_TEMPLATE_ID]
    print(f"  ✓ Visit outcomes: {len(visit_results)} configured")

    # Fetch first page of leads and stop
    leads = []
    for i, lead in enumerate(iter_leads(page_size=10)):
        leads.append(lead)
        if i >= 4:
            break
    print(f"  ✓ Leads (first 5 most-recently-active):")
    for l in leads:
        print(f"      {l['id']}  visits={l.get('visitsCount', 0):>3}  last={l.get('lastActivityTime', '')[:10]}")

    print("\nSPOTIO client is operational.")
