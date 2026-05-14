# HubSpot Migration Cleanup — New Session Handoff

*Created: May 14, 2026*

## Context

WTG migrated from Pipedrive → HubSpot. The migration parked Pipedrive data in `migrated_*` properties instead of HubSpot's standard / clean custom properties. Our reporting pipeline currently has to read from these `migrated_*` properties, which is brittle and not the long-term path. This session is for fixing the upstream data so going forward, all deals (migrated and net-new) populate the clean properties.

---

## What's broken

| Field used today (`migrated_*`) | Field we want to use (clean) | Status |
|---|---|---|
| `migrated_wontime` | `closed_won_time` (custom) or `hs_closed_won_date` (standard) | `closed_won_time` empty for current deals — workflow not populating it |
| `migrated_primary_lead_source` | `primary_lead_source` (or whatever the clean property is named) | Need to confirm if a clean version exists |
| `migrated_marketer_assigned__dentist_referral` | `marketer_assigned` | Clean property exists but empty |
| `migrated_zip_code` | `zip_code` | Clean property exists but empty |
| `migrated_general_dentist__city__phone_number` | `general_dentist` | Clean property exists but empty |

**Verified via API**: `closed_won_time` returns 0 deals for May 4-13, 2026, while `migrated_wontime` returns 287 in the same window. Same pattern for the others.

---

## Why this matters

Court doesn't want to maintain `migrated_*` references in the reporting code going forward. Once Pipedrive sync is fully cut over (June 1, 2026), the migration artifacts will become a dead-end:
- New deals created natively in HubSpot won't populate `migrated_*` fields
- Pipedrive will stop sending updates after June 1
- Workflows that *should* be copying data from `migrated_*` → clean fields aren't running

**The fix**: build HubSpot workflows (or fix existing ones) that:
1. When a deal is created/updated, copy `migrated_<field>` → `<clean_field>` if the clean field is empty
2. Going forward, new automations write directly to clean fields, not migrated ones
3. Once all data is mirrored, the `migrated_*` properties can be archived (not deleted yet — keep for audit)

---

## Specific properties to investigate

### Deal-level
- `migrated_wontime` (date) → target = `closed_won_time` (date) OR `hs_closed_won_date` (datetime, auto)
- `migrated_primary_lead_source` (enum?) → target = `primary_lead_source` (custom, need to verify exists)
- `migrated_marketer_assigned__dentist_referral` (string) → target = `marketer_assigned`
- `migrated_zip_code` (string) → target = `zip_code`
- `migrated_general_dentist__city__phone_number` (string) → target = `general_dentist`
- `migrated_close_time` (datetime) → target = `closedate` (standard)
- `migrated_first_won_time` (datetime) → target = (maybe combine into `closed_won_time`?)
- `migrated_pipeline` (label "Pipeline pipedrive") → target = standard `pipeline` (verify mapping)

### Company-level (already mostly clean)
- Standard `zip` is populated ✓
- `market` is populated ✓
- `market2` (fine territory like "Dallas SW") is populated ✓
- No major migration issues on company side

---

## Investigation script (run this to see current state)

```python
import os, requests
from pathlib import Path

for line in Path('.env').read_text().splitlines():
    if line.startswith('HUBSPOT_TOKEN='):
        os.environ['HUBSPOT_TOKEN'] = line.split('=',1)[1].strip().strip('"').strip("'")
        break
h = {'Authorization': f'Bearer {os.environ["HUBSPOT_TOKEN"]}'}

# For each (migrated, clean) pair, check if clean is populated
pairs = [
    ('migrated_wontime', 'closed_won_time'),
    ('migrated_zip_code', 'zip_code'),
    ('migrated_marketer_assigned__dentist_referral', 'marketer_assigned'),
    ('migrated_general_dentist__city__phone_number', 'general_dentist'),
    ('migrated_primary_lead_source', 'primary_lead_source'),
]

for mig, clean in pairs:
    r = requests.get('https://api.hubapi.com/crm/v3/objects/deals',
                     headers=h, params={'limit': 50, 'properties': f'{mig},{clean}'})
    mig_count = clean_count = 0
    for d in r.json().get('results', []):
        p = d.get('properties', {})
        if p.get(mig): mig_count += 1
        if p.get(clean): clean_count += 1
    print(f"{mig:50s} → {clean:30s}  populated: {mig_count}/50 → {clean_count}/50")
```

---

## Deliverables for this new session

1. **Map of every migrated_* property and its clean target** (confirm names + types match)
2. **HubSpot workflow(s)** that backfill clean fields from migrated_* values (one-time or ongoing)
3. **Verification**: sample 50 deals after the workflow runs — clean fields should match migrated values
4. **Update reporting code** (in the wtg-reports repo) to use clean field names — see [GitHub issue #1](https://github.com/Wisdom-Teeth-Guys/wtg-reports/issues/1)
5. **Document the new "source of truth" fields** so future work doesn't reintroduce migrated_* references

---

## Where to start

1. Read this file
2. Read `PROJECT_STATUS.md` in the same repo for full reporting pipeline context
3. Open HubSpot → Workflows → search for "migrate" or "lead source" — see what's already set up
4. Run the investigation script above to see real-time state of each property
5. For each gap, build/fix the workflow that should populate the clean property

---

## Related

- **Reporting repo**: https://github.com/Wisdom-Teeth-Guys/wtg-reports
- **GitHub issue tracking this**: https://github.com/Wisdom-Teeth-Guys/wtg-reports/issues/1
- **Current data flow** (uses migrated_*): `sync_hubspot_deals.py` in this repo
- **Target cutover**: June 1, 2026 (when Pipedrive turns off)

---

## Quick prompt for the new Claude session

> "Hey Claude — read `HUBSPOT_MIGRATION_FIX.md` at the root of the wtg-reports repo. We're cleaning up HubSpot properties so our reporting can stop relying on `migrated_*` field names. Start with the investigation script to see current state, then we'll build the workflows."
