"""
Shared configuration for the MMC Weekly Route Builder.

All constants, field name mappings, and lookup tables live here so other modules
import a single source of truth.
"""
import os

# ----------------------------------------------------------------------------
# File paths
# ----------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
TERRITORY_ZIP_MAP_JSON = os.path.join(PROJECT_ROOT, "territory_zip_map.json")
# Legacy alias: some external code still imports TERRITORY_ZIP_MAP_CSV.
TERRITORY_ZIP_MAP_CSV = TERRITORY_ZIP_MAP_JSON
# Optional: {territory: {subzone_name: [zip5, ...]}} — enables weekly zone rotation
# for driving efficiency. If absent, the whole territory is one zone (no rotation).
TERRITORY_SUBZONES_JSON = os.path.join(PROJECT_ROOT, "territory_subzones.json")
ROUTE_BUILDER_DIR = os.path.join(PROJECT_ROOT, "route_builder")
DATA_DIR = os.path.join(ROUTE_BUILDER_DIR, "data")
OUTPUT_DIR = os.path.join(ROUTE_BUILDER_DIR, "output")
OVERRIDES_DIR = os.path.join(ROUTE_BUILDER_DIR, "overrides")

# ----------------------------------------------------------------------------
# Scoring constants
# ----------------------------------------------------------------------------
TIER_WEIGHT = {"VIP": 40, "T1": 30, "T2": 20, "T3": 10, "Zero": 0}
TIER_CADENCE_DAYS = {"VIP": 14, "T1": 21, "T2": 30, "T3": 50, "Zero": 9999}

BOOST_FALLOFF = 30
BOOST_DORMANT = 20
BOOST_NEW_ORG = 15
BOOST_REPEAT_MISS = 10  # consecutive_closed_count >= 2

MAX_SCORE = 100
MAX_NEIGHBOR_MILES = 45.0
ORGS_PER_REP = 50

# ----------------------------------------------------------------------------
# Cadence-based selection rules (used by select_by_cadence)
# ----------------------------------------------------------------------------
# A tier is "due" if days-since-last-visit >= the value below.
# None means "no mandatory cadence" — used as priority fill or leftover.
TIER_CADENCE_RULES = {
    "VIP":    28,   # monthly
    "Tier 1": 28,   # monthly
    "Tier 2": 42,   # 6 weeks
    "Tier 3": 42,   # 6 weeks
    "Tier 4": None, # priority fill — most overdue first
    "Zero":   None, # leftover only
}
# Untiered orgs with at least this many lifetime won deals are promoted to T4.
PROMOTE_TO_T4_MIN_WINS = 1

# ----------------------------------------------------------------------------
# HubSpot field internal names (Company object)
# ----------------------------------------------------------------------------
HS_FIELDS = {
    # Route scoring
    "priority_score":   "visit_priority_score",
    "week_of":          "visit_week_of",
    "day_monday":       "visit_monday",
    "day_tuesday":      "visit_tuesday",
    "day_wednesday":    "visit_wednesday",
    "day_thursday":     "visit_thursday",
    "day_friday":       "visit_friday",
    "visit_reason":     "visit_reason",
    "tier_current":     "tier_current",
    "last_visit":       "last_visit_date",
    "last_won":         "last_won_deal_date",
    "t12m_wins":        "t12m_wins",
    "lifetime_wins":    "lifetime_won_deals",
    "falloff_flag":     "falloff_flag",
    "dormant_flag":     "dormant_flag",
    # Visit intelligence
    "last_outcome":     "last_visit_outcome",
    "best_window":      "best_visit_window",
    "lunch_closes":     "office_closes_for_lunch",
    "closed_fridays":   "office_closed_fridays",
    "key_contact":      "key_contact_name",
    "consec_closed":    "consecutive_closed_count",
    # Prioritization-v2 signals (see ROUTE_PRIORITIZATION.md)
    "tier_previous":          "tier_previous",
    "tier_dropped_date":      "tier_dropped_date",
    "diagnostic_logged":      "diagnostic_logged",
    "diagnostic_logged_date": "diagnostic_logged_date",
    "t12m_refs":              "t12m_refs",
    "t12m_conversion_rate":   "t12m_conversion_rate",
    "conversion_flag":        "conversion_flag",
    "lifetime_refs":          "lifetime_referral_count",
    "one_and_done_winner":    "is_one_and_done_winner",
    "top_loss_reason":        "top_loss_reason",
    "requires_visit_task":    "requires_visit_task",
}

# Full HubSpot field schema for `setup_visit_fields.py`.
# Tuple: (internal_name, label, type, fieldType, options_list_or_None)
HS_FIELD_SCHEMA = [
    ("visit_priority_score",       "Visit Priority Score",     "number",      "number",          None),
    ("visit_week_of",              "Visit Week Of",            "date",        "date",            None),
    # Per-day scheduling — one of these gets the specific date for the office's day this week.
    # MMC saved filters ("Monday", "Tuesday" etc) filter on these fields.
    ("visit_monday",               "Visit Monday",             "date",        "date",            None),
    ("visit_tuesday",              "Visit Tuesday",            "date",        "date",            None),
    ("visit_wednesday",            "Visit Wednesday",          "date",        "date",            None),
    ("visit_thursday",             "Visit Thursday",           "date",        "date",            None),
    ("visit_friday",               "Visit Friday",             "date",        "date",            None),
    ("visit_reason",               "Visit Reason",             "string",      "text",            None),
    ("tier_current",               "Tier Current",             "enumeration", "select",
        ["VIP", "T1", "T2", "T3", "Zero"]),
    ("last_visit_date",            "Last Visit Date",          "date",        "date",            None),
    ("last_won_deal_date",         "Last Won Deal Date",       "date",        "date",            None),
    ("t12m_wins",                  "T12M Wins",                "number",      "number",          None),
    ("falloff_flag",               "Falloff Flag",             "bool",        "booleancheckbox", None),
    ("dormant_flag",               "Dormant Flag",             "bool",        "booleancheckbox", None),
    # --- Visit Intelligence ---
    # NOTE: when adding new outcomes here, the schema-setup script uses the
    # value strings as both internal name and label. The HubSpot enum has
    # additional/cleaned labels — see the live property (extended 2026-06-05).
    ("last_visit_outcome",         "Last Visit Outcome",       "enumeration", "select",
        ["contacted_dm", "contacted_front_desk", "left_materials",
         "closed_retry", "closed_unknown", "closed_permanent",
         "appt_scheduled", "appt_only",
         "office_not_here", "duplicate_office", "does_own_extractions",
         "not_dental_office", "residential_address", "not_interested_6mo",
         "new_office", "phone_call", "mailed_swag"]),
    ("best_visit_window",          "Best Visit Window",        "enumeration", "select",
        ["morning", "midday", "afternoon", "unknown"]),
    ("office_closes_for_lunch",    "Closes for Lunch",         "bool",        "booleancheckbox", None),
    ("office_closed_fridays",      "Closed Fridays",           "bool",        "booleancheckbox", None),
    ("key_contact_name",           "Key Contact Name",         "string",      "text",            None),
    ("consecutive_closed_count",   "Consecutive Closed Count", "number",      "number",          None),
    # --- Prioritization v2 signals (see ROUTE_PRIORITIZATION.md) ---
    ("tier_previous",              "Tier Previous",            "enumeration", "select",
        ["VIP", "T1", "T2", "T3", "Zero"]),
    ("tier_dropped_date",          "Tier Dropped Date",        "date",        "date",            None),
    ("diagnostic_logged",          "Diagnostic Logged",        "bool",        "booleancheckbox", None),
    ("diagnostic_logged_date",     "Diagnostic Logged Date",   "date",        "date",            None),
    ("t12m_refs",                  "T12M Referrals",           "number",      "number",          None),
    ("t12m_conversion_rate",       "T12M Conversion %",        "number",      "number",          None),
    ("conversion_flag",            "Conversion Flag",          "bool",        "booleancheckbox", None),
    ("lifetime_referral_count",    "Lifetime Referrals",       "number",      "number",          None),
    ("is_one_and_done_winner",     "One-and-Done Winner",      "bool",        "booleancheckbox", None),
    ("top_loss_reason",            "Top Loss Reason",          "string",      "text",            None),
    ("requires_visit_task",        "Has Open Visit Task",      "bool",        "booleancheckbox", None),
]

# ----------------------------------------------------------------------------
# Visit outcome classification
# ----------------------------------------------------------------------------
# HubSpot enum values
OUTCOME_CONTACTED_DM = "contacted_dm"
OUTCOME_CONTACTED_FD = "contacted_front_desk"
OUTCOME_LEFT_MATERIALS = "left_materials"
OUTCOME_CLOSED_RETRY = "closed_retry"
OUTCOME_CLOSED_UNKNOWN = "closed_unknown"
OUTCOME_CLOSED_PERMANENT = "closed_permanent"
OUTCOME_APPT_SCHEDULED = "appt_scheduled"
OUTCOME_APPT_ONLY = "appt_only"
# Granular outcomes — added 2026-06-05 so each SPOTIO result maps to its own
# value instead of being bucketed into closed_permanent. Lets ops see WHY an
# office is disqualified, not just THAT it is.
OUTCOME_OFFICE_NOT_HERE = "office_not_here"
OUTCOME_DUPLICATE_OFFICE = "duplicate_office"
OUTCOME_DOES_OWN_EXTRACTIONS = "does_own_extractions"
OUTCOME_NOT_DENTAL_OFFICE = "not_dental_office"
OUTCOME_RESIDENTIAL_ADDRESS = "residential_address"
OUTCOME_NOT_INTERESTED_6MO = "not_interested_6mo"
OUTCOME_NEW_OFFICE = "new_office"
OUTCOME_PHONE_CALL = "phone_call"
OUTCOME_MAILED_SWAG = "mailed_swag"

# All disqualification outcomes (suppress from routes — office is gone, wrong,
# or fundamentally not a candidate). Treated equivalently to closed_permanent
# by the route builder.
SUPPRESSED_OUTCOMES = {
    OUTCOME_CLOSED_PERMANENT,
    OUTCOME_OFFICE_NOT_HERE,
    OUTCOME_DUPLICATE_OFFICE,
    OUTCOME_DOES_OWN_EXTRACTIONS,
    OUTCOME_NOT_DENTAL_OFFICE,
    OUTCOME_RESIDENTIAL_ADDRESS,
}

CLOSED_OUTCOMES = {
    OUTCOME_CLOSED_RETRY, OUTCOME_CLOSED_UNKNOWN, OUTCOME_APPT_ONLY,
    OUTCOME_NOT_INTERESTED_6MO,
} | SUPPRESSED_OUTCOMES
CONTACTED_OUTCOMES = {
    OUTCOME_CONTACTED_DM, OUTCOME_CONTACTED_FD, OUTCOME_LEFT_MATERIALS,
    OUTCOME_APPT_SCHEDULED, OUTCOME_NEW_OFFICE,
}

# SPOTIO `resultId` (int as str) → HubSpot last_visit_outcome value.
# Built from /api/activityResults (template 1 = Visit) — 41 entries total, ~23 active.
SPOTIO_RESULT_TO_OUTCOME = {
    # ---- Contacted (someone was actually present) ----
    "23": OUTCOME_CONTACTED_DM,            # Spoke with Dentist/Left Cards
    "1":  OUTCOME_CONTACTED_FD,            # Left Swag/Treats/Full Pitch
    "33": OUTCOME_CONTACTED_FD,            # Left Swag/Full Pitch
    "37": OUTCOME_CONTACTED_FD,            # Left Treats/Full Pitch
    "52": OUTCOME_CONTACTED_FD,            # Left Cards/Full Pitch
    "53": OUTCOME_CONTACTED_FD,            # Already Have Cards/Full Pitch
    "34": OUTCOME_NEW_OFFICE,              # New Office - First Visit
    # ---- Left materials (no contact made) ----
    "29": OUTCOME_LEFT_MATERIALS,          # Desk Empty/Left Cards
    "43": OUTCOME_LEFT_MATERIALS,          # MISCELLANEOUS CHECK-IN
    # ---- Closed — will retry ----
    "54": OUTCOME_CLOSED_RETRY,            # Office Closed/Left Cards
    "55": OUTCOME_CLOSED_RETRY,            # Office Closed/No Cards Left
    # ---- Disqualified (suppress from routes — granular reasons) ----
    "31": OUTCOME_CLOSED_PERMANENT,        # Office Permanently Closed
    "36": OUTCOME_OFFICE_NOT_HERE,         # Office Not Here Anymore
    "39": OUTCOME_DUPLICATE_OFFICE,        # Duplicate Office/Lost
    "47": OUTCOME_DOES_OWN_EXTRACTIONS,    # Office Does Own Extractions
    "49": OUTCOME_NOT_DENTAL_OFFICE,       # Not Dental Office
    "50": OUTCOME_RESIDENTIAL_ADDRESS,     # Residential Address
    # ---- Appointment scheduled ----
    "20": OUTCOME_APPT_SCHEDULED,          # Breakfast Drop
    "22": OUTCOME_APPT_SCHEDULED,          # Lunch & Learn
    # ---- Not interested (dormant, retry in 6 months) ----
    "35": OUTCOME_NOT_INTERESTED_6MO,      # Not Interested/6 months
    # ---- Non-visit touches (still log the outcome, but excluded from
    #      visit-frequency calculations via NON_VISIT_RESULT_IDS below) ----
    "56": OUTCOME_PHONE_CALL,              # Phone Call
    "57": OUTCOME_MAILED_SWAG,             # Mailed Swag/Cards
}

# SPOTIO results that are not actual field visits (skip when computing
# last_visit_date / consecutive_closed_count, but still log to last_visit_outcome
# if no actual visit has happened since).
NON_VISIT_RESULT_IDS = {"56", "57"}

# ----------------------------------------------------------------------------
# SPOTIO API
# ----------------------------------------------------------------------------
SPOTIO_BASE_URL = "https://api.spotio2.com"
SPOTIO_TOKEN_ENDPOINT = "/api/users/apitoken"
SPOTIO_USER_AGENT = "Mozilla/5.0 SpotioIntegration/1.0"
SPOTIO_VISIT_TEMPLATE_ID = "1"     # /api/activityTemplates — id=1 is the Visit template

# ----------------------------------------------------------------------------
# HubSpot API
# ----------------------------------------------------------------------------
HS_BASE_URL = "https://api.hubapi.com"

# Existing HubSpot properties we read but don't create (already populated by the team)
HS_EXISTING_FIELDS = {
    "territory":         "market2",            # label is "Territory"; internal is market2
    "market":            "market",             # legacy "Market" enum
    "marketer_assigned": "marketer_assigned",  # which rep covers this org
}


def load_env() -> dict:
    """Parse the project .env file and return a dict of vars.
    Strips whitespace and surrounding single/double quotes from values.
    """
    env = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env
