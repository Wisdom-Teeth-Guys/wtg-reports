"""
Order-independent address signature for matching SPOTIO leads to HubSpot companies.

Problem: SPOTIO stores addresses like "656 East 11400 South" (number first),
HubSpot stores them like "East 11400 South 656" (number last). Same place,
different word order.

Approach: extract house number from start OR end (never middle), normalize
suffix and directional words, return a (zip, house_number, frozenset(words))
tuple that's identical regardless of source.
"""
import re
from typing import Optional


_SUFFIX_MAP = {
    "street": "st", "st.": "st",
    "avenue": "ave", "av": "ave", "av.": "ave",
    "boulevard": "blvd",
    "road": "rd",
    "drive": "dr",
    "lane": "ln",
    "parkway": "pkwy", "pky": "pkwy",
    "highway": "hwy",
    "court": "ct",
    "place": "pl",
    "circle": "cir",
    "trail": "trl",
    "terrace": "ter",
    "expressway": "expy",
    "freeway": "fwy",
}

_DIR_MAP = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw",
    "southeast": "se", "southwest": "sw",
}

_SUITE_PATTERN = re.compile(r"\b(suite|ste|unit|apt|#)\s*\w*", re.IGNORECASE)
_NONWORD_PATTERN = re.compile(r"[^\w\s]")
_HOUSE_NUM_BAD_TOKEN = re.compile(r"^\d+$")


def normalize_zip(z) -> str:
    """Return a 5-digit ZIP string (handles ZIP+4 and stray whitespace)."""
    return str(z or "").strip()[:5]


def address_signature(addr: Optional[str], zip_code) -> Optional[tuple]:
    """Build the order-independent signature.

    Returns (zip5, house_num_str, frozenset(street_words)) or None if the
    address can't be parsed or zip is missing.
    """
    if not addr:
        return None
    # Drop city/state/zip if appended (after first comma)
    street = addr.split(",")[0]
    # Remove suite/unit info — varies between sources
    street = _SUITE_PATTERN.sub("", street)
    s = street.lower()
    s = _NONWORD_PATTERN.sub(" ", s)
    tokens = s.split()
    if not tokens:
        return None

    # House number lives at the start (SPOTIO style) OR the end (HubSpot style),
    # but never in the middle (those would be street-grid numbers like "11400").
    house = None
    if _HOUSE_NUM_BAD_TOKEN.match(tokens[0]):
        house = tokens[0]
        tokens = tokens[1:]
    elif _HOUSE_NUM_BAD_TOKEN.match(tokens[-1]):
        house = tokens[-1]
        tokens = tokens[:-1]
    else:
        return None

    # Normalize each remaining word
    norm = []
    for t in tokens:
        if t in _DIR_MAP:
            norm.append(_DIR_MAP[t])
        elif t in _SUFFIX_MAP:
            norm.append(_SUFFIX_MAP[t])
        else:
            norm.append(t)

    z = normalize_zip(zip_code)
    if not z:
        return None
    return (z, house, frozenset(norm))


def build_hs_index(hs_companies: list[dict]) -> dict:
    """Index a list of HubSpot company records by address signature.

    Returns: {signature: [list_of_companies_matching_that_sig]}
    """
    index = {}
    for c in hs_companies:
        p = c.get("properties") or {}
        sig = address_signature(p.get("address"), p.get("zip"))
        if sig:
            index.setdefault(sig, []).append(c)
    return index


def find_match(lead: dict, hs_index: dict) -> tuple:
    """Match a SPOTIO lead against a pre-built HubSpot signature index.

    Returns (status, hs_company_or_None) where status is one of:
        "unique"     — exactly 1 HubSpot company at this signature (use it)
        "ambiguous"  — 2+ HubSpot companies share this signature
        "missing"    — no signature could be built or no HubSpot match
    """
    pin = lead.get("pin") or {}
    sig = address_signature(pin.get("address"), pin.get("zip"))
    if not sig:
        return "missing", None
    candidates = hs_index.get(sig)
    if not candidates:
        return "missing", None
    if len(candidates) == 1:
        return "unique", candidates[0]
    return "ambiguous", None
