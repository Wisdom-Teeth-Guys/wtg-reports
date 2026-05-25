# WTG Reports

Weekly pipeline reporting for Wisdom Teeth Guys.

## How it works

```
HubSpot API → sync script → Google Sheet → Looker Studio → wtg-reporting.com
                  ↑
        GitHub Actions runs this weekly (Mondays 6 AM CT)
```

## Setup (one-time)

Required GitHub Secrets (Settings → Secrets and variables → Actions):

| Secret | What it is |
|---|---|
| `HUBSPOT_TOKEN` | HubSpot Private App access token with deals/companies/owners read scope |
| `GOOGLE_SHEET_ID` | ID from the Google Sheet URL (the long string between `/d/` and `/edit`) |
| `GOOGLE_SA_JSON` | Full JSON of the Google Service Account key (paste contents as-is) |

The Service Account email must be **shared on the Google Sheet as Editor**.

## Manual trigger

Go to **Actions → Sync HubSpot to Google Sheets → Run workflow**.

## Run locally for testing

```bash
pip install -r requirements.txt
export HUBSPOT_TOKEN=...
export GOOGLE_SHEET_ID=...
export GOOGLE_SA_JSON="$(cat path/to/service-account.json)"
python scripts/sync_hubspot_deals.py
```

## PHI / deal-data guardrails

GitHub is not HIPAA-compliant, so this repo enforces a three-layer wall against
patient PII and deal-specific CRM data:

1. **Local pre-commit hook** — `git commit` runs `scripts/phi_scan.py` and
   refuses commits that contain emails, phone numbers, SSNs, HubSpot CRM
   record URLs, HubSpot object IDs, or raw data file types (`*.csv`,
   `*.parquet`, etc.). Install once:
   ```bash
   pip install pre-commit
   pre-commit install
   ```
2. **`phi-scan` GitHub Actions workflow** — re-runs the same scan on every
   push. Catches commits that bypassed the local hook with `--no-verify`.
3. **`sync.yml` gate** — the deploy workflow runs the scan as its first step,
   so a leak aborts the Cloudflare deploy before any files reach the live
   site.

### When the scanner flags something

```
phi_scan: BLOCKED — 1 finding(s)
  [email] some_file.html:42
    Email address — possible patient or contact PII
    match: 'jane.doe@example.com'
```

Choose the right resolution:

- **Real leak** → remove the data, rebuild the report from aggregates only.
  Do not push, even with `--no-verify` — the CI workflow will catch it.
- **False positive** → add a unique substring of the match to
  `.phi-allowlist` (one per line, `#` for comments). Only allowlist after
  reviewing.
- **Disallowed file type** → keep the file in the working dir
  (`Claude Projects WTG/`), not this repo.

Run the scanner ad-hoc with `python3 scripts/phi_scan.py` (no args scans all
tracked files) or `python3 scripts/phi_scan.py path/to/file.html`.
