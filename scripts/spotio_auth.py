#!/usr/bin/env python3
"""
Spotio 2.0 API auth helper.

Spotio uses an API-key → JWT exchange:
  POST https://api.spotio2.com/api/users/apitoken
    body: {"clientId": "...", "secret": "..."}
    returns: {"accessToken": "<JWT>"}

Then use `Authorization: Bearer <JWT>` for subsequent calls.
The JWT expires after ~30 days based on observed `exp` field; caller should
cache it to disk and re-mint when expired.

Env vars required:
  SPOTIO_CLIENT_ID
  SPOTIO_API_SECRET   (also accepts SPOTIO_API_TOKEN as alias)

Discovered endpoints (May 15, 2026):
  /api/users/apitoken             — auth (working)
  /api/v2/activities              — POST, requires Bearer (returns 404 until correct body discovered)
  /api/auth/token                 — alt auth path (not currently used)

API reference: https://developer.spotio2.com
"""

from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests


SPOTIO_BASE = "https://api.spotio2.com"
TOKEN_ENDPOINT = f"{SPOTIO_BASE}/api/users/apitoken"
TOKEN_CACHE = Path(__file__).resolve().parent.parent / ".spotio_token_cache.json"


def _decode_jwt_payload(token: str) -> dict:
    """Cheap JWT decode (no signature check) just to read the exp claim."""
    import base64
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        # Pad base64 properly
        payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def _is_token_valid(token: str) -> bool:
    claims = _decode_jwt_payload(token)
    exp = claims.get("exp")
    if not exp:
        return False
    # Refresh if expiring within next 60 seconds
    return datetime.now(timezone.utc).timestamp() < (exp - 60)


def _load_cached_token() -> Optional[str]:
    if not TOKEN_CACHE.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE.read_text())
        tok = data.get("accessToken")
        return tok if tok and _is_token_valid(tok) else None
    except Exception:
        return None


def _save_token(token: str) -> None:
    TOKEN_CACHE.write_text(json.dumps({"accessToken": token}))


def get_spotio_token(force_refresh: bool = False) -> str:
    """Returns a valid Spotio JWT, minting a new one if cache is empty/expired."""
    if not force_refresh:
        cached = _load_cached_token()
        if cached:
            return cached

    client_id = os.environ.get("SPOTIO_CLIENT_ID")
    secret = (
        os.environ.get("SPOTIO_API_SECRET")
        or os.environ.get("SPOTIO_API_TOKEN")
        or os.environ.get("SPOTIO_API")
    )
    if not client_id or not secret:
        raise RuntimeError(
            "Set SPOTIO_CLIENT_ID and SPOTIO_API_SECRET (or SPOTIO_API_TOKEN/SPOTIO_API) "
            "environment variables."
        )

    r = requests.post(
        TOKEN_ENDPOINT,
        json={"clientId": client_id, "secret": secret},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    token = r.json().get("accessToken")
    if not token:
        raise RuntimeError(f"No accessToken in response: {r.text[:300]}")
    _save_token(token)
    return token


def auth_headers() -> dict:
    """Convenience: returns headers dict with Bearer token ready to use."""
    return {
        "Authorization": f"Bearer {get_spotio_token()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


if __name__ == "__main__":
    # CLI: print a fresh token for debugging
    force = "--force" in sys.argv
    tok = get_spotio_token(force_refresh=force)
    claims = _decode_jwt_payload(tok)
    exp = claims.get("exp")
    if exp:
        exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
        print(f"Token expires: {exp_dt:%Y-%m-%d %H:%M UTC}")
    print(f"Token: {tok[:50]}…{tok[-20:]}")
