#!/usr/bin/env python3
"""
PHI / deal-data scanner for wtg-reports.

Walls off three classes of risk before code reaches GitHub:
  1. PHI: emails, phone numbers, SSNs.
  2. Deal-specific CRM data: HubSpot object IDs (10+ digit numerics in CRM-shaped
     contexts) and HubSpot CRM record URLs.
  3. Raw data dumps: csv / parquet / xlsx / sqlite files, and anything under
     cache/, data/, or audit/ directory trees.

Usage:
  python scripts/phi_scan.py [PATH ...]

If no paths are given, scans every file tracked by git.

Exits 0 if clean, 1 if anything is flagged. Findings are printed with file,
line number, rule name, and the offending excerpt.

An allowlist of known-safe substrings can live at .phi-allowlist (one
substring per line, '#' for comments). If a match contains an allowlisted
substring, it is skipped.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- Rules ------------------------------------------------------------------
# Each rule: (name, compiled regex, human-readable description)
# Regexes are intentionally tight to keep false-positive rate near zero. The
# scanner is a safety net; the real defense is keeping PHI out of the build
# pipeline upstream.

RULES = [
    (
        "email",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        "Email address — possible patient or contact PII",
    ),
    (
        "phone",
        re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"),
        "Phone number — possible patient or contact PII",
    ),
    (
        "ssn",
        re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
        "SSN-shaped number",
    ),
    (
        "hubspot_crm_url",
        re.compile(r"hubspot\.com/(?:contacts|deals|companies)/\d+/record/\d+", re.IGNORECASE),
        "HubSpot CRM record URL — leaks a specific deal/contact/company",
    ),
    (
        "hubspot_id_field",
        re.compile(r"(?:hs_object_id|hs_deal_id|dealId|contactId|companyId)\s*[=:]\s*['\"]?\d{6,}", re.IGNORECASE),
        "HubSpot object ID assignment — single-record CRM reference",
    ),
]

# Filenames / globs that must never be committed.
DISALLOWED_NAME_PATTERNS = [
    (re.compile(r"\.csv$", re.IGNORECASE), "CSV file — likely raw data export"),
    (re.compile(r"\.parquet$", re.IGNORECASE), "Parquet file — likely raw data export"),
    (re.compile(r"\.xlsx?$", re.IGNORECASE), "Excel file — likely raw data export"),
    (re.compile(r"\.sqlite3?$", re.IGNORECASE), "SQLite DB — likely raw data export"),
    (re.compile(r"\.tsv$", re.IGNORECASE), "TSV file — likely raw data export"),
]

# Path-prefix bans (relative to repo root). Anything under these trees should
# stay in the working dir, not the deploy repo.
DISALLOWED_PATH_PREFIXES = [
    ("marketer_reports/cache/", "marketer_reports/cache/ contains raw HubSpot pulls"),
    ("marketer_reports/data/", "marketer_reports/data/ contains raw HubSpot pulls"),
    ("marketer_reports/scripts/audit/", "audit/ contains per-deal investigation output"),
]

# File extensions to scan with the regex rules. Binary / image / known-safe
# types are skipped to keep the scan fast and avoid false positives in
# minified JS or compiled assets.
SCAN_EXTENSIONS = {".html", ".htm", ".md", ".yaml", ".yml", ".py", ".json", ".txt", ".js", ".css"}

# Paths the scanner should ignore even if they match other criteria.
SCANNER_SELF_PATHS = {
    "scripts/phi_scan.py",
    ".pre-commit-config.yaml",
    ".github/workflows/phi-scan.yml",
    ".phi-allowlist",
}


def load_allowlist() -> list[str]:
    path = REPO_ROOT / ".phi-allowlist"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def git_tracked_files() -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "ls-files"], cwd=REPO_ROOT, text=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [line for line in out.splitlines() if line]


def normalize(paths: list[str]) -> list[str]:
    """Convert any absolute paths into repo-relative, drop missing files."""
    out = []
    for p in paths:
        ap = Path(p)
        if ap.is_absolute():
            try:
                rel = ap.relative_to(REPO_ROOT)
            except ValueError:
                continue
            p = str(rel)
        if (REPO_ROOT / p).is_file():
            out.append(p)
    return out


def scan_file(path: str, allowlist: list[str]) -> list[tuple[str, int, str, str]]:
    """Return a list of (rule_name, line_no, description, excerpt) findings."""
    findings: list[tuple[str, int, str, str]] = []
    full = REPO_ROOT / path

    # Filename rules
    name = full.name
    for pattern, desc in DISALLOWED_NAME_PATTERNS:
        if pattern.search(name):
            findings.append(("disallowed_filetype", 0, desc, path))

    # Path-prefix rules
    for prefix, desc in DISALLOWED_PATH_PREFIXES:
        if path.startswith(prefix):
            findings.append(("disallowed_path", 0, desc, path))

    # Skip content scan for non-text or out-of-scope file types
    ext = full.suffix.lower()
    if ext not in SCAN_EXTENSIONS:
        return findings

    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings

    for line_no, line in enumerate(text.splitlines(), start=1):
        for rule_name, pattern, desc in RULES:
            for m in pattern.finditer(line):
                excerpt = m.group(0)
                if any(allow in excerpt for allow in allowlist):
                    continue
                findings.append((rule_name, line_no, desc, excerpt))

    return findings


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args:
        paths = normalize(args)
    else:
        paths = git_tracked_files()

    # Always exclude scanner-self files
    paths = [p for p in paths if p not in SCANNER_SELF_PATHS]

    allowlist = load_allowlist()

    all_findings: list[tuple[str, str, int, str, str]] = []
    for path in paths:
        for rule_name, line_no, desc, excerpt in scan_file(path, allowlist):
            all_findings.append((path, rule_name, line_no, desc, excerpt))

    if not all_findings:
        print(f"phi_scan: clean ({len(paths)} files scanned)")
        return 0

    print(f"phi_scan: BLOCKED — {len(all_findings)} finding(s)\n")
    for path, rule_name, line_no, desc, excerpt in all_findings:
        loc = f"{path}:{line_no}" if line_no else path
        print(f"  [{rule_name}] {loc}")
        print(f"    {desc}")
        print(f"    match: {excerpt!r}")
        print()
    print(
        "To resolve:\n"
        "  • Real leak → remove the data, rebuild the report from aggregates only.\n"
        "  • False positive → add a unique substring to .phi-allowlist (one per line).\n"
        "  • Disallowed file type / path → keep that file in the working dir, not this repo.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
