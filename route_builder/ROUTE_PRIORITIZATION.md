# Route Prioritization Spec

This is the **authoritative rule book** for how the weekly route builder selects the ~30 offices each marketer should visit. The current `scoring.py` is a simplified v1; this spec defines the full v2 model.

The order below is the **priority order** — band 1 is selected before band 2, etc. Within each band, ties break on `priority_score` (computed) descending.

---

## Tiering rule (locked)

Tiers are **wins-only** — the three-way (wins / refs / weighted) model is retired. Two passes; the final tier is the **better of the two** for each org.

### Pass 1 — Absolute (T12M wins)

| Tier | T12M wins |
|---|---|
| VIP | 20 + |
| T1  | 11 – 19 |
| T2  | 5 – 10 |
| T3  | 1 – 4 |
| Zero | 0 |

### Pass 2 — Per-market percentile (thin-market fallback)

For each territory, rank orgs by T12M wins descending. **Only orgs with ≥1 T12M win are eligible** (Zero-win orgs are never percentile-promoted).

| Tier | Percentile within territory |
|---|---|
| VIP | top 5 % |
| T1  | next 10 % (5 – 15 %) |
| T2  | next 25 % (15 – 40 %) |
| T3  | next 30 % (40 – 70 %) |
| Zero | rest |

### Final tier

`final_tier = max(absolute_tier, percentile_tier)` — best of both, where `VIP > T1 > T2 > T3 > Zero`.

This guarantees thin markets (Tucson, Houston Southeast, etc.) always have a real VIP / T1 even when no one hits the absolute thresholds; strong markets are unaffected because absolute usually wins or ties.

**Example — Tucson with only 258 orgs:**
- Best office has 7 T12M wins → Pass 1: T2 → Pass 2 (rank 0 of N): VIP → **Final: VIP**
- 12 orgs have ≥1 win → Pass 2 distributes them: 1 VIP, 1 T1, 3 T2, 4 T3, 3 Zero (by percentile bands)

**Example — Dallas Southwest (strong market):**
- Top office has 28 wins → Pass 1: VIP → Pass 2 also VIP → **Final: VIP**
- 80 orgs have ≥1 win → percentile gives 4 VIP candidates, but absolute already named ~6 → no change

Implementation: `route_builder/tier_refresh.py` — `assign_tier_absolute()`, `assign_tier_percentile()`, `best_tier()`. Tunable constants `TIER_THRESHOLDS` and `TIER_PERCENTILES` live at the top of the same file.

---

## Priority bands (in order)

### Band 1 — Tier-dropped, no diagnostic logged
**Why:** A VIP or T1 that just lost tier status is the most urgent signal in the territory. Something just changed, and we don't yet know what. Diagnose before relationships erode.

**Selection criteria:**
- `tier_previous` is one of `VIP`, `T1`
- `tier_current` is lower than `tier_previous` (i.e. they dropped)
- `tier_dropped_date` is within the last 60 days
- `diagnostic_logged` is `false` (no one has visited and recorded a "why" yet)

**Visit reason shown to rep:** `"Dropped from {tier_previous} on {date} — diagnose"`

---

### Band 2 — VIP / T1 overdue on cadence
**Why:** Our biggest referrers; missing their cadence window erodes relationship.

**Selection criteria:**
- `tier_current` in `[VIP, T1]`
- `days_since_last_visit` > `tier_cadence_days` (VIP: 14, T1: 21)
- (already in v1 scoring)

**Visit reason:** `"{tier} overdue {N}d"`

---

### Band 3 — Any office flagged dipping
**Why:** Referral volume or win rate has dropped vs baseline. Catch early before they hit Band 1.

**Selection criteria:**
- `falloff_flag` (current name; we may rename to `dipping_flag` for clarity)
- ANY tier — VIP through T3

**Visit reason:** `"Dipping vs baseline"`

---

### Band 4 — VIP / T1 hitting cadence window this week
**Why:** Stay ahead of overdue. If a VIP's last visit was day 7 and cadence is 14, schedule the visit this week — don't wait until day 15.

**Selection criteria:**
- `tier_current` in `[VIP, T1]`
- `cadence_target_days - days_since_last_visit` is between `0` and `7`
- NOT already overdue (that's Band 2)

**Visit reason:** `"{tier} due in {N}d"`

---

### Band 5 — High-referral / low-conversion (with mechanism context)
**Why:** They believe in us enough to send patients, but something is breaking down between referral and close. Marketer's job is relationship reinforcement + diagnostic on the mechanism (call-center? scheduling? insurance friction?).

**Selection criteria:**
- `t12m_refs` >= 5 (meaningful referral volume)
- `t12m_conversion_rate` < 0.40 (win rate below 40%)
- `conversion_flag` (computed monthly)

**Visit reason:** `"Conv {pct}% on {N} refs — {top_loss_reason}"`

Example: `"Conv 28% on 11 refs — most lost at call-center hold"`

The "mechanism context" requires pulling top loss reason from the deals associated with this office. See **Data gaps → loss-reason aggregation** below.

---

### Band 6 — One-and-done winners
**Why:** They referred exactly once, it converted to a won deal, and they've gone silent. Highest unrealized potential in the dataset — they know we work, they just stopped sending.

**Selection criteria:**
- `lifetime_referral_count` == 1
- That single referral became a Won deal
- No referral activity in the last 180 days

**Visit reason:** `"1-for-1 winner — never referred again"`

---

### Band 7 — Lower-tier overdue
**Why:** T2/T3 cadence matters too, just less urgent than VIP/T1.

**Selection criteria:**
- `tier_current` in `[T2, T3]`
- `days_since_last_visit` > `tier_cadence_days` (T2: 30, T3: 50)

**Visit reason:** `"{tier} overdue {N}d"`

---

### Band 8 — Open tasks requiring a visit
**Why:** Anything a manager (or workflow automation) flagged for in-person follow-up. Doesn't have to be tier-related — could be a new contact intro, a competitor-sighting follow-up, a complaint resolution.

**Selection criteria:**
- HubSpot has an open task associated with the company AND the task subject contains a visit keyword (`visit`, `drop by`, `stop by`, `in-person`, `face-to-face`)
- OR the task has a custom `requires_visit` flag

**Visit reason:** `"Open task: {task_subject}"`

---

### Band 9 — Geographic fill
**Why:** Once the priority bands above produce N orgs (where N ≤ 30), fill remaining slots with offices geographically clustered near existing stops. Catches drive-by opportunities without adding miles.

**Selection criteria:**
- All orgs not already selected
- Within 5 miles of any already-selected stop in the same day's cluster (MMC handles the day-of clustering; we just feed candidates)
- `tier_current` not empty (skip "Zero" tier — those are unverified leads)
- `last_visit_outcome` ≠ `closed_permanent`

**Visit reason:** `"Geo-fill near {nearest_priority_org}"`

---

## Hard suppressions (apply before any band)

These offices NEVER appear in routes regardless of band rules:

| Condition | Field |
|---|---|
| Office permanently closed | `last_visit_outcome` == `closed_permanent` |
| Not a dental office | resolved via SPOTIO outcome 49 / 50 → `closed_permanent` |
| Duplicate of another HubSpot company | resolved via SPOTIO outcome 39 → `closed_permanent` |
| Marketer-excluded for this week | row in `overrides/overrides_YYYY-MM-DD.csv` with `action=EXCLUDE` |

## Hard inclusions (apply after band selection)

These offices ALWAYS appear in the route regardless of bands:

| Condition | Source |
|---|---|
| Force-include override for this week | row in `overrides/overrides_YYYY-MM-DD.csv` with `action=FORCE_IN` |
| `consecutive_closed_count` >= 3 with no `closed_permanent` outcome | manager review flag — surface for triage |

---

## Selection algorithm (per territory, per week)

```
target = 30   # ORGS_PER_REP from config

selected = []
for band in [1, 2, 3, 4, 5, 6, 7, 8]:
    candidates = orgs_matching(band) - selected - hard_suppressed
    add to selected until either:
      - selected reaches target
      - candidates for this band are exhausted
    (within band, sort by priority_score desc)

# Fill remaining with geo-near candidates (Band 9)
if len(selected) < target:
    selected += geo_fill_candidates(selected, target - len(selected))

# Apply force-includes (always — even if it pushes past 30)
for o in force_include_overrides(territory, week):
    if o not in selected:
        selected.insert(0, o)
```

Output: a per-territory CSV identical to v1 plus a new `band` column showing which rule pulled the office in.

---

## Data signals — what we have today vs what's missing

| Signal | Field / source | Status |
|---|---|---|
| `tier_current` | HubSpot custom field (created) | ✅ field exists, **empty** — needs `tier_refresh.py` to populate |
| `tier_previous` | NEW HubSpot field | ⛔ **Not yet created** — add via `setup_visit_fields.py` |
| `tier_dropped_date` | NEW HubSpot field | ⛔ **Not yet created** |
| `diagnostic_logged` | NEW HubSpot field (boolean) | ⛔ **Not yet created** + needs MMC check-in option "logged diagnostic" |
| `last_visit_date` | HubSpot custom field (created) | ✅ populated by `spotio_backfill.py` and ongoing MMC sync |
| `falloff_flag` (rename to `dipping_flag`?) | HubSpot custom field (created) | ✅ field exists, **empty** — needs automation |
| `t12m_refs` | NEW HubSpot field OR computed from HubSpot deals | ⛔ Currently we only track `t12m_wins` |
| `t12m_conversion_rate` | Computed: `t12m_wins / t12m_refs` | ⛔ Needs `t12m_refs` first |
| `conversion_flag` | NEW HubSpot field | ⛔ |
| `lifetime_referral_count` | NEW HubSpot field OR computed | ⛔ |
| `is_one_and_done_winner` | NEW HubSpot field | ⛔ |
| Open tasks per company | HubSpot Tasks API | ⛔ `hubspot_client.py` doesn't yet pull tasks |
| Lat / lng for geo fill | SPOTIO `pin.lat`/`pin.lng` per lead | ✅ available via SPOTIO; just not wired in |
| Top loss reason for an office | HubSpot deal pipeline + `migrated_lostreason` (or similar) | 🟡 unclear if populated; needs audit |

---

## New HubSpot fields to add

Drop these into `HS_FIELD_SCHEMA` in `config.py` and run `setup_visit_fields.py --push` to create them:

| Internal Name | Label | Type | Set by |
|---|---|---|---|
| `tier_previous` | Tier Previous | Dropdown (VIP/T1/T2/T3/Zero) | `tier_refresh.py` |
| `tier_dropped_date` | Tier Dropped Date | Date | `tier_refresh.py` |
| `diagnostic_logged` | Diagnostic Logged | Checkbox | MMC check-in (new field) |
| `diagnostic_logged_date` | Diagnostic Logged Date | Date | MMC check-in (auto) |
| `t12m_refs` | T12M Referrals | Number | `tier_refresh.py` (Pipedrive) |
| `t12m_conversion_rate` | T12M Conversion % | Number | `tier_refresh.py` (computed) |
| `conversion_flag` | Conversion Flag | Checkbox | `tier_refresh.py` |
| `lifetime_referral_count` | Lifetime Referrals | Number | `tier_refresh.py` |
| `is_one_and_done_winner` | One-and-Done Winner | Checkbox | `tier_refresh.py` |
| `top_loss_reason` | Top Loss Reason | Single-line text | `tier_refresh.py` |
| `requires_visit_task` | Has Open Visit Task | Checkbox | nightly task sweeper |

That's **11 new fields** on top of the 15 already created (total 26 fields managing the routing layer).

---

## Implementation order (recommended)

1. **Phase A — what we can do with existing data (this week):**
   - Land Bands 2, 4, 7 (cadence-based — only needs `tier_current` + `last_visit_date`)
   - Land Band 3 if `falloff_flag` can be backfilled from historical data
   - Geo fill (Band 9) using SPOTIO lat/lng

2. **Phase B — after tier_refresh.py runs against Pipedrive (week 2):**
   - Land Band 5 (high-ref / low-conv) once `t12m_refs` and `conversion_flag` are populated
   - Land Band 6 (one-and-done winners) — needs lifetime referral count

3. **Phase C — after new MMC check-in fields go live (week 3+):**
   - Land Band 1 (tier-dropped diagnostic) — needs `tier_previous` to be tracked AND MMC's "diagnostic logged" check-in option

4. **Phase D — HubSpot task integration (week 4):**
   - Land Band 8 (open tasks) — needs `hubspot_client.py` extended to pull tasks per company

---

## MMC additions needed for the new bands

Beyond the 5 check-in fields already specified in the ops runbook, MMC needs:

| New MMC Check-in Field | Type | Required | HubSpot target |
|---|---|---|---|
| **Diagnostic logged?** | Yes/No toggle | Recommended after Band 1 visits | `diagnostic_logged` |
| **Diagnostic notes** | Free text (optional) | No | stored as MMC note + synced to a HubSpot note (no field) |

The diagnostic toggle should default to **No** and only flip to **Yes** when the rep actually had a substantive conversation about why the tier dropped. Don't make it required (that defeats the purpose — we want reps to be honest about when they did vs didn't diagnose).

---

## Tie-breaking within a band

If a band produces more candidates than there are slots remaining, sort by a refined `priority_score`:

```
within-band score
  + 5 per day overdue (so most-overdue wins ties)
  + 3 per tier rank (VIP=4, T1=3, T2=2, T3=1)
  + 2 if consecutive_closed_count >= 2 (rep needs intel)
  + 1 if `key_contact_name` is empty (we don't even know who to ask for)
```

This ensures the worst-data orgs and the longest-overdue orgs surface first within any given band.

---

## Worked example (one rep, one week)

Say Brooklyn's territory (Dallas Southwest) has these populated signals on Monday morning:

```
Pool: 1,415 orgs in territory
Hard-suppressed: 12 (closed_permanent + EXCLUDE overrides)
Active pool: 1,403

Band 1 — dropped no diagnostic:        2 orgs   → ALL included
Band 2 — VIP/T1 overdue:               8 orgs   → ALL included  (10/30)
Band 3 — dipping_flag:                 4 orgs   → ALL included  (14/30)
Band 4 — VIP/T1 due this week:         6 orgs   → ALL included  (20/30)
Band 5 — high-ref low-conv:            3 orgs   → ALL included  (23/30)
Band 6 — one-and-done winners:         5 orgs   → ALL included  (28/30)
Band 7 — T2/T3 overdue:               47 orgs   → top 2 by score (30/30)
Band 8 — open visit tasks:             1 org    → force-included (31/30 ← exceeds cap)
Band 9 — geo fill:                              → SKIPPED (target reached)

Final route: 31 orgs (one over due to hard-include from Band 8)
```

Output CSV gets a new `band` column so the manager can see which rule pulled each row in. That's also what the audit report reads from to compute per-band compliance (e.g. "we hit 100% of Band 1 visits this week but only 60% of Band 5").

---

## Open questions

1. **What's a "diagnostic"?** Does it need to be a structured form, or is "rep had a 5-min real conversation and tagged it Yes" enough?
2. **What's the right `t12m_refs` threshold for Band 5?** I've defaulted to 5+ refs and <40% conversion — adjust based on actual distribution.
3. **Should `dipping_flag` be a manual call by the territory manager, or auto-set by `tier_refresh.py` when month-over-month referrals drop >X%?**
4. **Band 8 — what kind of tasks count?** Just open HubSpot tasks, or also tasks from other systems (Dialpad call logs, Eric's manual lists)?
5. **Lifetime referral data freshness** — Pipedrive has the truth for old deals, HubSpot has new deals after migration. Where does `lifetime_referral_count` get computed?
