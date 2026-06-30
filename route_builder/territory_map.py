"""
Load and query the territory → ZIP mapping.

`territory_zip_map.csv` columns: Territory, Zip Code
"""
import csv
from typing import Optional

from .config import TERRITORY_ZIP_MAP_CSV


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def load_territory_zip_map(csv_path: str = TERRITORY_ZIP_MAP_CSV) -> dict[str, set[str]]:
    """Return {territory_name_lowercase: {zip5, zip5, ...}}.

    Handles BOTH wide-format CSV (territories as columns) and long-format
    (Territory + Zip Code as two columns). Wide format is what's currently
    in territory_zip_map.csv.
    """
    out: dict[str, set[str]] = {}
    with open(csv_path) as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return out
        # Detect format: if header has exactly 2 cols with names like Territory/Zip Code → long
        is_long = (
            len(header) == 2
            and any(h.strip().lower() in ("territory", "market") for h in header)
            and any(h.strip().lower() in ("zip", "zip code", "postal code") for h in header)
        )
        if is_long:
            # Reset and use DictReader
            f.seek(0)
            r = csv.DictReader(f)
            for row in r:
                t = _norm(row.get("Territory") or row.get("Market") or "")
                z = (row.get("Zip Code") or row.get("Zip") or "").strip()[:5]
                if t and z:
                    out.setdefault(t, set()).add(z)
        else:
            # Wide format: each header cell is a territory name; rows hold ZIPs per column
            territory_names = [h.strip() for h in header]
            for row in reader:
                for i, cell in enumerate(row):
                    z = (cell or "").strip()[:5]
                    if i < len(territory_names) and z and territory_names[i]:
                        out.setdefault(_norm(territory_names[i]), set()).add(z)
    return out


def load_zip_to_territory(csv_path: str = TERRITORY_ZIP_MAP_CSV) -> dict[str, str]:
    """Return {zip5: territory_name_canonical_case}. Last one wins on collision."""
    mapping = load_territory_zip_map(csv_path)
    # Recover original-case territory names from the header
    with open(csv_path) as f:
        header = next(csv.reader(f), [])
    name_lookup = {_norm(h): h.strip() for h in header}
    out: dict[str, str] = {}
    for lower_name, zips in mapping.items():
        canonical = name_lookup.get(lower_name, lower_name.title())
        for z in zips:
            out[z] = canonical
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
