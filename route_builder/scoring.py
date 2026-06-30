"""
Pure scoring logic. No I/O. Fully unit-testable.

Score formula (capped at 100):
    score = tier_weight
          + overdue_factor      # = min((days_since_last_visit / target_cadence) * 20, 20)
          + 30 if falloff_flag
          + 20 if dormant_flag  (180+ days no visit AND 3+ lifetime wins)
          + 15 if new_org       (<90 days old, never visited)
          + 10 if consecutive_closed_count >= 2

Permanent closures are filtered before scoring — they never appear in routes.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .config import (
    BOOST_DORMANT, BOOST_FALLOFF, BOOST_NEW_ORG, BOOST_REPEAT_MISS,
    MAX_SCORE, SUPPRESSED_OUTCOMES, TIER_CADENCE_DAYS, TIER_WEIGHT,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class OrgRecord:
    """Normalized in-memory representation of a HubSpot company for scoring."""
    hs_id: str
    name: str = ""
    zip: str = ""
    city: str = ""
    state: str = ""
    address: str = ""
    territory: str = ""
    # Scoring inputs
    tier: str = ""
    last_visit_date: Optional[date] = None
    last_won_date: Optional[date] = None
    t12m_wins: int = 0
    lifetime_wins: int = 0
    falloff_flag: bool = False
    dormant_flag: bool = False
    create_date: Optional[date] = None
    # Visit intelligence inputs
    last_visit_outcome: str = ""
    best_visit_window: str = ""
    office_closes_for_lunch: bool = False
    office_closed_fridays: bool = False
    key_contact_name: str = ""
    consecutive_closed_count: int = 0
    # Scoring outputs (filled by score_org)
    score: int = 0
    visit_reason: str = ""
    days_overdue: int = 0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def is_new_org(create_date: Optional[date], today: date,
               threshold_days: int = 90) -> bool:
    if not create_date:
        return False
    return (today - create_date).days < threshold_days


def compute_overdue_factor(last_visit_date: Optional[date], tier: str, today: date) -> float:
    """Return the overdue contribution (0..20)."""
    cadence = TIER_CADENCE_DAYS.get(tier, 0)
    if not cadence or cadence >= 9999:
        return 0.0
    if not last_visit_date:
        # Never visited → treat as fully overdue
        return 20.0
    days_since = max((today - last_visit_date).days, 0)
    factor = (days_since / cadence) * 20.0
    return min(factor, 20.0)


def score_org(org: OrgRecord, today: date) -> OrgRecord:
    """Score a single org IN PLACE — sets .score, .visit_reason, .days_overdue.

    Disqualified orgs (closed permanently, not a dental office, duplicate, etc.)
    return score=0 with reason describing why. Caller should filter these out
    before route selection.
    """
    # Suppress disqualified offices
    if org.last_visit_outcome in SUPPRESSED_OUTCOMES:
        org.score = 0
        org.visit_reason = f"SUPPRESSED — {org.last_visit_outcome.replace('_', ' ')}"
        org.days_overdue = 0
        return org

    parts = []
    tier = org.tier or "Zero"
    tier_w = TIER_WEIGHT.get(tier, 0)
    score = float(tier_w)
    if tier_w:
        parts.append(tier if tier != "Zero" else "Zero-tier")

    overdue = compute_overdue_factor(org.last_visit_date, tier, today)
    if org.last_visit_date:
        org.days_overdue = max((today - org.last_visit_date).days, 0)
        if overdue >= 1.0:
            parts.append(f"Overdue {org.days_overdue}d")
    else:
        # Never visited
        if tier_w:
            parts.append("Never visited")
    score += overdue

    if org.falloff_flag:
        score += BOOST_FALLOFF
        parts.append("Falloff")
    if org.dormant_flag:
        score += BOOST_DORMANT
        parts.append("Dormant")
    if is_new_org(org.create_date, today):
        score += BOOST_NEW_ORG
        parts.append("New org")
    if org.consecutive_closed_count >= 2:
        score += BOOST_REPEAT_MISS
        parts.append(f"Closed {org.consecutive_closed_count}x in a row")

    org.score = min(int(round(score)), MAX_SCORE)
    org.visit_reason = " | ".join(parts) if parts else "No signal"
    return org


def score_all(orgs: list[OrgRecord], today: date) -> list[OrgRecord]:
    for o in orgs:
        score_org(o, today)
    return orgs


def select_top_n(orgs: list[OrgRecord], n: int, today: date,
                 force_include_ids: Optional[set[str]] = None,
                 exclude_ids: Optional[set[str]] = None) -> list[OrgRecord]:
    """Score, filter, and return the top n orgs for a territory.

    - Permanently-closed and explicit-excludes are removed first.
    - force_include_ids are always kept (placed at the top of the result).
    """
    force_include_ids = force_include_ids or set()
    exclude_ids = exclude_ids or set()

    # Filter
    active = [
        o for o in orgs
        if o.hs_id not in exclude_ids
        and o.last_visit_outcome not in SUPPRESSED_OUTCOMES
    ]
    score_all(active, today)
    active.sort(key=lambda o: o.score, reverse=True)

    # Pull force-includes to the top
    forced = [o for o in active if o.hs_id in force_include_ids]
    rest = [o for o in active if o.hs_id not in force_include_ids]

    selected = forced + rest
    return selected[:n]


def _days_since(d: Optional[date], today: date) -> int:
    """Days since d (or a very large number if d is None / never visited)."""
    if not d:
        return 10**6
    return max((today - d).days, 0)


def _promote_untiered_with_wins(orgs: list[OrgRecord]) -> None:
    """Mutate orgs in-place: any org with no tier but >=1 lifetime win → Tier 4."""
    from .config import PROMOTE_TO_T4_MIN_WINS
    for o in orgs:
        if (not o.tier) and o.lifetime_wins >= PROMOTE_TO_T4_MIN_WINS:
            o.tier = "Tier 4"


def select_by_cadence(orgs: list[OrgRecord], n: int, today: date,
                      force_include_ids: Optional[set[str]] = None,
                      exclude_ids: Optional[set[str]] = None) -> list[OrgRecord]:
    """Cadence-based selection (replaces score-based for weekly routes).

    Algorithm:
      0. Filter out closed_permanent + excluded.
      1. Promote untiered-with-wins to Tier 4.
      2. Pass A — Mandatory: any org whose tier has a cadence rule and is overdue
         (days_since_last_visit >= rule, OR never visited). Sorted most-overdue
         first; capped at n.
      3. Pass B — Priority fill: if slots remain, pull Tier 4 orgs (most overdue
         first; never-visited counts as overdue).
      4. Pass C — Leftover: if slots STILL remain, pull Zero (most overdue first).
      5. force_include_ids jump to the top of the final list.

    Side effect: each selected org gets a `visit_reason` reflecting the bucket.
    """
    from .config import TIER_CADENCE_RULES
    force_include_ids = force_include_ids or set()
    exclude_ids = exclude_ids or set()

    # 0. Filter
    active = [
        o for o in orgs
        if o.hs_id not in exclude_ids
        and o.last_visit_outcome not in SUPPRESSED_OUTCOMES
    ]
    # 1. Promote
    _promote_untiered_with_wins(active)
    # Also run scoring so the CSV exports retain a score for inspection
    score_all(active, today)

    # Build helper: days overdue under a tier's rule (None if no rule)
    def overdue_days(o: OrgRecord) -> Optional[int]:
        rule = TIER_CADENCE_RULES.get(o.tier)
        if rule is None: return None
        d = _days_since(o.last_visit_date, today)
        return d - rule  # positive = overdue

    # 2a. ROLLOVER pass — falloff_flag=true means we missed it last week, auto-include.
    selected: list[OrgRecord] = []
    seen: set[str] = set()
    rollover = [o for o in active if getattr(o, "falloff_flag", False)]
    for o in rollover:
        if len(selected) >= n: break
        o.visit_reason = f"ROLLOVER: missed last week ({o.tier or 'Zero'})"
        selected.append(o); seen.add(o.hs_id)

    # 2b. Mandatory cadence pass
    mandatory = [(overdue_days(o), o) for o in active if overdue_days(o) is not None and overdue_days(o) >= 0]
    mandatory.sort(key=lambda x: -x[0])  # most overdue first

    for days, o in mandatory:
        if len(selected) >= n: break
        if o.hs_id in seen: continue
        o.visit_reason = f"{o.tier} overdue by {days}d" + (" (never visited)" if not o.last_visit_date else "")
        selected.append(o); seen.add(o.hs_id)

    # 3. Priority fill — Tier 4
    if len(selected) < n:
        t4 = [(_days_since(o.last_visit_date, today), o) for o in active
              if o.tier == "Tier 4" and o.hs_id not in seen]
        t4.sort(key=lambda x: -x[0])
        for days, o in t4:
            if len(selected) >= n: break
            o.visit_reason = (f"Tier 4 priority fill ({days}d since visit)"
                              if o.last_visit_date else
                              "Tier 4 priority fill (never visited)")
            selected.append(o); seen.add(o.hs_id)

    # 4. Leftover — Zero
    if len(selected) < n:
        zeros = [(_days_since(o.last_visit_date, today), o) for o in active
                 if o.tier == "Zero" and o.hs_id not in seen]
        zeros.sort(key=lambda x: -x[0])
        for days, o in zeros:
            if len(selected) >= n: break
            o.visit_reason = (f"Zero leftover ({days}d since visit)"
                              if o.last_visit_date else
                              "Zero leftover (never visited)")
            selected.append(o); seen.add(o.hs_id)

    # 5. force_include jumps to top
    forced = [o for o in active if o.hs_id in force_include_ids and o.hs_id not in seen]
    for o in forced:
        o.visit_reason = "FORCE_IN override"
    return forced + selected[: n - len(forced)] if forced else selected
