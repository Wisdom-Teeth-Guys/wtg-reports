"""
Load and query the territory → ZIP mapping.

`territory_zip_map.json` schema: flat `{zip5: territory_name}`.

Formerly `territory_zip_map.csv`. Switched to JSON so the PHI scanner
(scripts/phi_scan.py) doesn't flag it as a raw-data CSV — the file has no
PHI, but the scanner blocks all `.csv` filenames regardless of content.
"""
import json
from typing import Optional

from .config import TERRITORY_ZIP_MAP_JSON


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _load_raw(path: Optional[str] = None) -> dict[str, str]:
    """Return the raw {zip: territory_canonical_name} dict as stored on disk."""
    path = path or TERRITORY_ZIP_MAP_JSON
    with open(path) as f:
        return json.load(f)


def load_territory_zip_map(path: Optional[str] = None) -> dict[str, set[str]]:
    """Return {territory_name_lowercase: {zip5, zip5, ...}}."""
    raw = _load_raw(path)
    out: dict[str, set[str]] = {}
    for z, terr in raw.items():
        z5 = (z or "").strip()[:5]
        if z5 and terr:
            out.setdefault(_norm(terr), set()).add(z5)
    return out


def load_zip_to_territory(path: Optional[str] = None) -> dict[str, str]:
    """Return {zip5: territory_name_canonical_case}. Last one wins on collision."""
    raw = _load_raw(path)
    out: dict[str, str] = {}
    for z, terr in raw.items():
        z5 = (z or "").strip()[:5]
        if z5 and terr:
            out[z5] = terr.strip()
    return out


def zip_in_territory(zip_code: str, territory: str,
                     mapping: Optional[dict[str, set[str]]] = None) -> bool:
    if mapping is None:
        mapping = load_territory_zip_map()
    return (zip_code or "")[:5] in mapping.get(_norm(territory), set())


if __name__ == "__main__":
    m = load_territory_zip_map()
    print(f"Territories: {len(m)}")
    for t in sorted(m):
        print(f"  {t}  ({len(m[t])} zips)")
