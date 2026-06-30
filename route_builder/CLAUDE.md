# Route Builder — Ops Runbook

The MMC Weekly Route Builder picks ~30 priority dental/ortho offices per territory each week, optionally writes the assignments to HubSpot, and lets MMC pick them up via its overnight HubSpot sync to build the geo-optimized daily routes.

**All scripts default to dry-run.** They only write to HubSpot when explicitly given `--push`.

---

## What's in this folder

| File | Purpose |
|---|---|
| `config.py` | Constants, scoring weights, field-name mappings, SPOTIO result → outcome map |
| `hubspot_client.py` | HubSpot REST API wrapper (stdlib urllib, no third-party deps) |
| `spotio_client.py` | SPOTIO API wrapper (JWT token exchange, leads, activities) |
| `address_matcher.py` | Order-independent address signature for SPOTIO ↔ HubSpot matching |
| `territory_map.py` | Loads `../territory_zip_map.csv` (wide format: territories as columns) |
| `scoring.py` | Pure scoring functions — no I/O. Unit tests in `tests/test_scoring.py` |
| `setup_visit_fields.py` | Creates the 15 HubSpot custom fields. Idempotent. |
| `spotio_backfill.py` | One-time historical SPOTIO → HubSpot intelligence backfill |
| `visit_intelligence_updater.py` | Weekly SPOTIO refresh (thin wrapper around `spotio_backfill.py`) |
| `build_weekly_routes.py` | **Main weekly orchestrator** (select top 30 per territory) |
| `route_audit_report.py` | Planned-vs-actual compliance report (run Friday after the week) |
| `tier_refresh.py` | Monthly tier recalc from Pipedrive deal data (skeleton — needs CSV format) |
| `tests/test_scoring.py` | Unit tests for the scoring engine |
| `output/` | Per-run CSV outputs (gitignored) |
| `overrides/` | Manager exception CSVs (FORCE_IN / EXCLUDE per week) |

---

## First-time setup (already done)

These steps were run during initial build. You don't need to re-run unless starting fresh:

```bash
# 1. Verify SPOTIO + HubSpot API access
python3 -m route_builder.spotio_client     # should print user/territory/lead counts
python3 -m route_builder.hubspot_client    # should print field status

# 2. Create the 15 HubSpot custom fields
python3 -m route_builder.setup_visit_fields              # dry-run preview
python3 -m route_builder.setup_visit_fields --push       # actually create
```

**Credentials needed in `../.env`:**
```
HUBSPOT_TOKEN=pat-na2-...
SPOTIO_CLIENT_ID=<from SPOTIO → Settings → Integrations → API Access>
SPOTIO_API_SECRET=<from same place>
```

---

## Weekly run (dry-run first, always)

```bash
cd "/Users/courtiorg/CLAUDE - WTG/Claude Projects WTG"

# DRY RUN — all 15 territories, fresh SPOTIO intel, no HubSpot writes
python3 -m route_builder.build_weekly_routes

# Output: route_builder/output/routes_YYYY-MM-DD/
#   Austin.csv, Dallas_Northeast.csv, ..., Utah_South.csv, _summary.txt
```

Review each territory's CSV (open in Numbers/Excel/your tool of choice). Each row is one office, with score, visit reason, last visit outcome from SPOTIO, etc.

**When you're happy with the selections, push to HubSpot:**

```bash
python3 -m route_builder.build_weekly_routes --push
```

This stamps `visit_week_of`, `visit_priority_score`, and `visit_reason` on the 450 selected companies (30 × 15 territories). MMC's overnight HubSpot sync will pick them up; the next morning, each rep sees their 30 in the MMC app.

---

## Common variations

```bash
# Just one territory (fast):
python3 -m route_builder.build_weekly_routes --territory "Dallas Southwest"

# Skip the SPOTIO refresh (uses whatever's already in HubSpot — much faster):
python3 -m route_builder.build_weekly_routes --no-spotio

# Target a specific Monday (default: next Monday):
python3 -m route_builder.build_weekly_routes --week-of 2026-06-01

# Cap SPOTIO scan for speed during testing:
python3 -m route_builder.build_weekly_routes --spotio-max-leads 500

# Just verify HubSpot has all 15 fields:
python3 -m route_builder.build_weekly_routes --check-fields
```

---

## Manager overrides

If a manager wants to force-include or exclude a specific office for a given week, drop a CSV into `route_builder/overrides/` named `overrides_YYYY-MM-DD.csv` (where the date is the target Monday).

```csv
territory,hs_id,org_name,action,reason
Dallas Southwest,12345678,Smile Dental,FORCE_IN,New relationship — manager priority
Austin,87654321,Happy Teeth,EXCLUDE,Closed for renovation through May
```

The route builder picks this up automatically if it exists. Actions:
- `FORCE_IN` — always include this org in the 30, even if score is low (gets ranked first)
- `EXCLUDE` — drop this org from this week's route

---

## Backfilling visit history from SPOTIO

If you need to repopulate the visit intelligence fields from SPOTIO (e.g. after a HubSpot data reset, or to pull in older history):

```bash
# DRY RUN — see what would be updated
python3 -m route_builder.spotio_backfill --since 2026-01-01

# PUSH — actually update HubSpot
python3 -m route_builder.spotio_backfill --since 2026-01-01 --push

# Output: route_builder/output/backfill_YYYY-MM-DD/
#   matched.csv   — orgs successfully linked SPOTIO → HubSpot
#   unmatched.csv — SPOTIO leads with visits we couldn't find in HubSpot
#   ambiguous.csv — SPOTIO leads where multiple HubSpot orgs matched
#   summary.txt   — counts + top repeat-closed offices
```

The backfill computes per org:
- `last_visit_outcome` — most recent visit's classified outcome
- `consecutive_closed_count` — how many visits in a row were found closed
- `last_visit_date` — most recent visit's date

---

## SPOTIO outcome mapping (so you understand what's flowing through)

| SPOTIO Result | → HubSpot `last_visit_outcome` | Counts as "Closed"? |
|---|---|---|
| Spoke with Dentist/Left Cards | `contacted_dm` | no |
| Left Swag/Treats/Full Pitch (+ 4 similar) | `contacted_front_desk` | no |
| Desk Empty/Left Cards, MISCELLANEOUS CHECK-IN | `left_materials` | no |
| Office Closed/Left Cards, Office Closed/No Cards Left | `closed_retry` | **yes** |
| Office Permanently Closed, Office Not Here Anymore, Duplicate Office/Lost, Not Dental Office, Residential Address, Office Does Own Extractions | `closed_permanent` | **yes — suppresses from routes** |
| Not Interested/6 months | `appt_only` | **yes** |
| Breakfast Drop, Lunch & Learn | `appt_scheduled` | no |
| Phone Call, Mailed Swag/Cards | (skipped — not field visits) | n/a |

If SPOTIO adds a new result type, update `SPOTIO_RESULT_TO_OUTCOME` in `config.py`.

---

## Scoring formula (`scoring.py`)

```
score = tier_weight                          # VIP=40, T1=30, T2=20, T3=10
      + min((days_overdue / target_cadence) * 20, 20)
      + 30 if falloff_flag
      + 20 if dormant_flag                   # 180+ days no visit AND 3+ lifetime wins
      + 15 if new_org                        # createdate < 90 days
      + 10 if consecutive_closed_count >= 2  # Brooklyn-pattern boost
capped at 100

If last_visit_outcome == "closed_permanent" → score=0, filtered out of routes
```

**Target cadence by tier (days):**
VIP=14, T1=21, T2=30, T3=50

---

## What to expect right now (May 2026)

The system works end-to-end, but **scoring will be sparse** until two things land:

1. **Tier assignments** — every org currently has empty `tier_current`. Until tier data is populated (manually or via `tier_refresh.py` once it's built), most orgs score only on overdue/falloff/closure signals.
2. **Full SPOTIO backfill** — only the test sample of 30 companies has `last_visit_outcome` populated. Running the full `spotio_backfill.py --push` will fill in the rest.

Once both are in place, scores will differentiate cleanly across tiers and the top 30 per territory will be meaningfully prioritized.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing SPOTIO_CLIENT_ID or SPOTIO_API_SECRET in .env` | Add the two lines to `../.env` (no quotes around values) |
| HubSpot 401 errors | Token in `.env` is expired/revoked. Regenerate `HUBSPOT_TOKEN` |
| SPOTIO `ClientId and Secret do not exist` | Credentials wrong. Regenerate from SPOTIO admin and update `.env` |
| Route CSVs are empty | Territory name doesn't match `territory_zip_map.csv` exactly (case-insensitive); check `--territory "..."` argument |
| All scores are 15 (`New org`) | Expected for newly-imported orgs — tier data hasn't been loaded yet |
| Match rate (SPOTIO → HubSpot) is low | Check `unmatched.csv` after backfill — likely HubSpot is missing `address` for many companies |

---

## Weekly + monthly cadence (recommended)

```
Sunday 7:30pm   visit_intelligence_updater.py --push
Sunday 8:00pm   build_weekly_routes.py --push
Friday 5:00pm   route_audit_report.py --week-of <last_monday>
Monthly         tier_refresh.py --pipedrive-file <export> --push
```

Wiring this into cron (or macOS launchd) is the only remaining setup step.

### Mid-week audit

After Friday, run the audit to see what actually got contacted (not just visited):

```bash
python3 -m route_builder.route_audit_report --week-of 2026-05-11
```

The **contact rate** is the key metric (not visit rate). Brooklyn's 2,160 visits had a 47% closed-door rate — visit count alone hides that; contact rate exposes it.

Optionally boost orgs that were planned but had no contact, so they outscore everything next week:

```bash
python3 -m route_builder.route_audit_report --week-of 2026-05-11 --apply-boost --push
```

### Monthly tier refresh

```bash
# 1. Drop the Pipedrive export into route_builder/data/
# 2. Run dry-run:
python3 -m route_builder.tier_refresh --pipedrive-file route_builder/data/Deal\ Won\ Time\ SHEET.xlsx

# 3. Review tier distribution + unmatched.csv
# 4. If happy, push:
python3 -m route_builder.tier_refresh --pipedrive-file route_builder/data/Deal\ Won\ Time\ SHEET.xlsx --push
```

The script auto-detects column names containing "organization"/"company"/"account" + "won date"/"close date".

**Tiering is wins-only with a two-pass rule (see ROUTE_PRIORITIZATION.md for details):**
- **Pass 1 (absolute):** VIP ≥ 20 T12M wins, T1 = 11–19, T2 = 5–10, T3 = 1–4
- **Pass 2 (per-market percentile):** ranks orgs within each territory; top 5% = VIP, next 10% = T1, next 25% = T2, next 30% = T3
- Final tier = **better** of the two passes (thin markets get a real VIP via percentile; strong markets unaffected)
- The three-way (wins/refs/weighted) comparison is retired

When a tier drops between runs (e.g. T1 → T2), the script auto-stamps `tier_previous` and `tier_dropped_date` so Band 1 of the route builder ("dropped, no diagnostic logged") picks them up next week.

---

## Future work (nice-to-haves, not blocking)

- `geo_filter.py` — drop geographic outliers in each territory (currently MMC handles this on its side; territory bucketing is sufficient)
- Cron / launchd setup for fully automated Sunday-night runs
- Per-rep dashboard view (rolls up audit data into rep scorecards)
