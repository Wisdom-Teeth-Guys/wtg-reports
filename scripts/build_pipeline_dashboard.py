import pandas as pd, numpy as np, json, math, csv, os
from datetime import datetime, timezone
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
OUT_DIR = Path(__file__).resolve().parent.parent / "out"
OUT_DIR.mkdir(exist_ok=True)
OUTPUT = str(OUT_DIR / "pipeline_dashboard.html")
UPDATE_DATE = datetime.now(timezone.utc).strftime('%B %-d, %Y')

GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SA_JSON  = os.environ["GOOGLE_SA_JSON"]

# Pipeline → Market (3-letter market code). Used for EXCL filter and as a fallback
# when a deal has no ZIP. The ZIP→Territory CSV is the primary source for fine-
# grained territory; the Pipeline-based 3-letter code is the parent Market.
PIPELINE_TO_MARKET = {
    'Phoenix - Wisdom Teeth Guys':'PHX','Tucson - Wisdom Teeth Guys':'PHX',
    'Dallas - Wisdom Teeth Guys':'DAL','Office Referrals - Dallas':'DAL',
    'Houston - Wisdom Teeth Guys':'HOU','Office Referrals - Houston':'HOU',
    'Austin - Wisdom Teeth Guys':'AUS','Office Referrals - Austin':'AUS',
    'San Antonio - Wisdom Teeth Guys':'SA','Office Referrals - San Antonio':'SA',
    'Utah - Wisdom Teeth Guys':'UT','Office Referrals - Utah':'UT','Bad Deals':'EXCL',
}
# Backwards-compat alias used elsewhere in the script
TERRITORY_MAP = PIPELINE_TO_MARKET
MARKETER_MARKET = {  # marketer → market (3-letter). Was MARKETER_TERRITORY.
    'Kaya Landers':'PHX','Armida Sanchez':'PHX','Megan Riely':'AUS',
    'Holly Chandler':'SA','Abigail Nilles':'HOU','Justin Padilla':'HOU',
    'Patricia Kriner':'HOU','Avery Brown':'HOU','Brittney Calhoun':'HOU',
    'Morgan Tondre':'HOU','Andrea Lobato':'UT','Eric Wade':'UT',
    'Helena Anderson':'UT','Chandra Niekamp':'UT','Kaylie Kamalu':'UT',
    'Chloe Christenson':'DAL','Brooklyn Hill':'DAL','Aaron McGaughey':'DAL',
    'Jessica Murray':'DAL','Val Zuniga':'DAL','Tonia Canova':'DAL',
    'Kathie Harden':'DAL','Kaytlin Patridge':'DAL','Carly Reps':'DAL','Shanel Slate':'DAL',
}
MARKETER_TERRITORY = MARKETER_MARKET  # legacy alias
MARKET_ORDER = ['AUS','DAL','HOU','PHX','SA','UT']
TERR_ORDER = MARKET_ORDER  # legacy alias

# Long-form market name → 3-letter code (HubSpot returns long names in `market`)
MARKET_LONG_TO_CODE = {
    'Austin':'AUS','Dallas':'DAL','Houston':'HOU','Phoenix':'PHX',
    'San Antonio':'SA','Tucson':'PHX',  # Tucson rolls into PHX market (per current TERRITORY_MAP)
    'Utah':'UT','Unknown':'UNK',
}

# ── Load data from Google Sheets ─────────────────────────────────────────────
print("Loading data from Google Sheet…")

def _load_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    client = gspread.authorize(creds)
    sh = client.open_by_key(GOOGLE_SHEET_ID)
    deals_rows = sh.worksheet("deals_raw").get_all_records()
    co_rows    = sh.worksheet("companies_raw").get_all_records()
    return deals_rows, co_rows

deals_rows, co_rows = _load_sheet()
print(f"  → {len(deals_rows)} deals, {len(co_rows)} companies")

# Convert to DataFrames
df_deals = pd.DataFrame(deals_rows)
df_co    = pd.DataFrame(co_rows)
df_co    = df_co.rename(columns={
    'name': 'company_name_lookup',
    'zip': 'company_zip_lookup',
    'market': 'company_market_lookup',
    'territory': 'company_territory_lookup',
})
df_deals['company_id'] = df_deals['company_id'].astype(str)
df_co['company_id']    = df_co['company_id'].astype(str)

# Join deals → companies
df = df_deals.merge(df_co, on='company_id', how='left')

# Derive market (3-letter code) from HubSpot company_market_lookup (preferred)
# or from territory text. HubSpot has values like "Phoenix", "Houston SE", "Dallas SW".
def _to_market_code(val):
    if val is None or pd.isna(val): return None
    s = str(val).strip().lower()
    if not s: return None
    if 'phoenix' in s or 'tucson' in s: return 'PHX'
    if 'dallas' in s:     return 'DAL'
    if 'houston' in s:    return 'HOU'
    if 'austin' in s:     return 'AUS'
    if 'san antonio' in s or s == 'sa': return 'SA'
    if 'utah' in s:       return 'UT'
    return None

# Try company market first, then deal-level territory
df['Market'] = df['company_market_lookup'].apply(_to_market_code)
df.loc[df['Market'].isna(), 'Market'] = df['territory'].apply(_to_market_code)

# ZIP-prefix → Market fallback (catches deals where company.market is missing)
def _zip_to_market(z):
    if z is None or pd.isna(z): return None
    s = str(z).strip().split('-')[0].split('.')[0].zfill(5)[:5]
    if not s.isdigit(): return None
    p2 = s[:2]
    if p2 in ('75','76'): return 'DAL'
    if p2 == '77':        return 'HOU'
    if p2 == '78':
        try:
            n = int(s[2:])
            return 'SA' if n < 600 else 'AUS'
        except: return None
    if p2 == '85':        return 'PHX'
    if p2 == '86' and s.startswith('857'): return 'PHX'
    if p2 in ('83','84'): return 'UT'
    return None

_z = df['company_zip_lookup'].fillna('').astype(str).where(lambda s: s != '', df['deal_zip'].fillna('').astype(str))
df.loc[df['Market'].isna(), 'Market'] = _z[df['Market'].isna()].apply(_zip_to_market)

# Final fallback: infer from marketer's most common market
_known = df.dropna(subset=['Market']).groupby('marketer_assigned')['Market'].agg(lambda s: s.mode().iloc[0] if len(s) > 0 else None).to_dict()
df.loc[df['Market'].isna(), 'Market'] = df.loc[df['Market'].isna(), 'marketer_assigned'].map(_known)

# Anything still unmapped → UNK
df['Market'] = df['Market'].fillna('UNK')

# Pipeline name (for CEO's pipeline-first filter view)
df['Pipeline'] = df.get('pipeline_name', '').fillna('').astype(str).str.strip()
df.loc[df['Pipeline'] == '', 'Pipeline'] = '(no pipeline)'

# Filter to Dentist Referral + Orthodontist Referral lead sources only
# (per Court May 14, 2026: this is the canonical filter, no more pipeline filtering)
_ALLOWED_LEAD_SOURCES = {'dentist referral', 'orthodontist referral'}
if 'primary_lead_source' in df.columns:
    df = df[df['primary_lead_source'].fillna('').astype(str).str.strip().str.lower().isin(_ALLOWED_LEAD_SOURCES)].copy()

# Map to the schema the rest of this script expects
df['Date']     = pd.to_datetime(df['create_date'], errors='coerce', utc=True).dt.tz_localize(None)
df = df[df['Date'].notna()].copy()
df['Marketer'] = df['marketer_assigned'].fillna('').astype(str).str.strip()
df.loc[(df['Marketer'] == '') | (df['Marketer'].str.lower().str.startswith('x')), 'Marketer'] = 'Unassigned'

_unknown_vals = {'na','n/a','n.a','n.a.','unknown','does not remember','none',''}
_gd  = df['general_dentist'].fillna('').astype(str)
_org = df['company_name_lookup'].fillna('').astype(str)
df['Org'] = _org.where(_org.str.strip().str.lower().apply(lambda x: x not in _unknown_vals and x != ''),
              _gd.where(_gd.str.strip().str.lower().apply(lambda x: x not in _unknown_vals and x != ''), 'Unknown'))

# Status: derive from won_time presence. Won if won_time set; Lost if close_date set but no won_time; else Open.
_won_time = pd.to_datetime(df['won_time'], errors='coerce', utc=True)
_close_dt = pd.to_datetime(df['close_date'], errors='coerce', utc=True)
df['Status'] = np.where(_won_time.notna(), 'Won',
                np.where(_close_dt.notna(), 'Lost', 'Open'))

df['Year']  = df['Date'].dt.year
df['Month'] = df['Date'].dt.month
df['YM']    = df['Date'].dt.to_period('M')

# ZIP normalization (kept for display in detail rows; no longer used for territory derivation)
def _norm_zip(v):
    if v is None or pd.isna(v): return None
    s = str(v).strip()
    if not s or s.lower() in ('nan','none','null'): return None
    s = s.split('-')[0].split('.')[0].strip()
    if not s.isdigit(): return None
    return s.zfill(5)[:5]

df['Zip'] = df['company_zip_lookup'].apply(_norm_zip).fillna(df['deal_zip'].apply(_norm_zip))

# Territory: use fine-grained company_territory ("Dallas SW") if present, else broad market name
df['Territory'] = df['company_territory_lookup'].fillna('').astype(str).str.strip()
df.loc[df['Territory'] == '', 'Territory'] = df['company_market_lookup'].fillna('').astype(str).str.strip()
df.loc[df['Territory'] == '', 'Territory'] = 'Unassigned'

_n_total      = len(df)
_n_unassigned = (df['Territory']=='Unassigned').sum()
print(f"Loaded {_n_total} deals; {_n_total - _n_unassigned} mapped to a territory ({_n_unassigned} Unassigned)")

# Build territory → market lookup from the data itself (most common market per territory)
terr_to_market = {}
zip_to_terr = {}
zip_to_market = {}
for terr, grp in df.groupby('Territory'):
    if grp['Market'].notna().any():
        terr_to_market[terr] = grp['Market'].mode().iloc[0]
    else:
        terr_to_market[terr] = 'UNK'

df_won    = df[df['Status'] == 'Won'].copy()
df_closed = df[df['Status'].isin(['Won','Lost'])].copy()

# T12M = trailing 12 months ending today
_today = pd.Timestamp(datetime.now(timezone.utc).date())
T12M_END   = _today
T12M_START = _today - pd.DateOffset(years=1) + pd.Timedelta(days=1)

# Rolling 12 complete months for heatmap: May '25 – Apr '26
# Last 12 fully-complete months
_last_complete = (_today.replace(day=1) - pd.Timedelta(days=1)).to_period('M')
_first_in_roll = (_today.replace(day=1) - pd.DateOffset(months=12)).to_period('M')
ROLL12 = list(pd.period_range(_first_in_roll, _last_complete, freq='M'))
ROLL12_LBL = [f"{p.strftime('%b')} '{str(p.year)[2:]}" for p in ROLL12]
# Prior year same months
ROLL12_PREV = list(pd.period_range('2024-05','2025-04', freq='M'))

def tier_label(w):
    if w>=20: return 'VIP'
    if w>=11: return 'Tier 1'
    if w>=5:  return 'Tier 2'
    return 'Tier 3'

# ── KPI aggregates (legacy — kept for older charts) ──────────────────────────
total_t12m  = len(df_won[(df_won['Date']>=T12M_START)&(df_won['Date']<=T12M_END)])
total_2025  = len(df_won[df_won['Year']==2025])
total_2024  = len(df_won[df_won['Year']==2024])
jf26_total  = len(df_won[(df_won['Year']==2026)&(df_won['Month'].isin([3,4]))])  # Mar+Apr '26
jf25_total  = len(df_won[(df_won['Year']==2025)&(df_won['Month'].isin([3,4]))])  # Mar+Apr '25
jf_yoy_pct  = round((jf26_total/jf25_total - 1)*100,1) if jf25_total else 0

# ── New KPI scorecards (Deals Created) ───────────────────────────────────────
# df['Date'] = create_date (this dashboard is "Referral Deals Created Pipeline")
_iso_today = _today.isocalendar()
_cur_iso_year, _cur_iso_week = _iso_today.year, _iso_today.week
_dates_iso = df['Date'].dt.isocalendar()

# This week vs last week
deals_this_week = int(((_dates_iso.year == _cur_iso_year) & (_dates_iso.week == _cur_iso_week)).sum())
if _cur_iso_week == 1:
    _last_iso_week = 52; _last_iso_year = _cur_iso_year - 1
else:
    _last_iso_week = _cur_iso_week - 1; _last_iso_year = _cur_iso_year
deals_last_week = int(((_dates_iso.year == _last_iso_year) & (_dates_iso.week == _last_iso_week)).sum())

# Last 90 days vs previous 90 days
_90 = _today - pd.Timedelta(days=90)
_180 = _today - pd.Timedelta(days=180)
deals_last_90    = int(((df['Date'] > _90)  & (df['Date'] <= _today)).sum())
deals_prev_90    = int(((df['Date'] > _180) & (df['Date'] <= _90)).sum())
deals_90_yoy_pct = round((deals_last_90 / deals_prev_90 - 1)*100, 1) if deals_prev_90 else 0

# Month-to-date this year vs same date range last year
_mtd_start  = _today.replace(day=1)
_mtd_ly_start = _mtd_start - pd.DateOffset(years=1)
_mtd_ly_end   = _today - pd.DateOffset(years=1)
deals_mtd     = int(((df['Date'] >= _mtd_start) & (df['Date'] <= _today)).sum())
deals_mtd_ly  = int(((df['Date'] >= _mtd_ly_start) & (df['Date'] <= _mtd_ly_end)).sum())
deals_mtd_pct = round((deals_mtd / deals_mtd_ly - 1)*100, 1) if deals_mtd_ly else 0

print(f"Created KPIs: this_week={deals_this_week}, last_week={deals_last_week}, "
      f"90d={deals_last_90} vs {deals_prev_90} ({deals_90_yoy_pct:+}%), "
      f"MTD={deals_mtd} vs LY {deals_mtd_ly} ({deals_mtd_pct:+}%)")

# ── Per-account data ─────────────────────────────────────────────────────────
print("Computing per-account metrics (grouped by org, not by marketer)…")
accounts = []
for (mkt,terr,org), grp in df_won.groupby(['Market','Territory','Org']):
    # Identify dominant marketer for this org (most won deals in the rolling window)
    _mk_counts = grp[grp['Date']>=T12M_START]['Marketer'].value_counts()
    if len(_mk_counts) > 0:
        mk = _mk_counts.index[0]
        other_count = max(0, len(_mk_counts) - 1)
        mk_display = f"{mk}" + (f" +{other_count}" if other_count else "")
    else:
        mk = 'Unassigned'
        mk_display = 'Unassigned'
    t12m  = len(grp[(grp['Date']>=T12M_START)&(grp['Date']<=T12M_END)])
    w2024 = len(grp[grp['Year']==2024])
    w2025 = len(grp[grp['Year']==2025])
    w2026 = len(grp[grp['Year']==2026])
    feb26 = len(grp[(grp['Year']==2026)&(grp['Month']==2)])
    mar26 = len(grp[(grp['Year']==2026)&(grp['Month']==3)])
    apr26 = len(grp[(grp['Year']==2026)&(grp['Month']==4)])
    feb25 = len(grp[(grp['Year']==2025)&(grp['Month']==2)])
    mar25 = len(grp[(grp['Year']==2025)&(grp['Month']==3)])
    apr25 = len(grp[(grp['Year']==2025)&(grp['Month']==4)])
    if t12m==0 and w2024==0: continue
    ma26_wins = mar26+apr26  # last 2 complete months of current year
    ma25_wins = mar25+apr25  # same 2 months prior year
    # Min-volume floor: <5 combined wins → no actionable alert (avoids flapping
    # on tiny accounts when sliced by fine-grained territory).
    if (ma25_wins + ma26_wins) < 5:
        alert, reason = 'Stable', ''
    elif ma25_wins==0 and ma26_wins==0:
        alert,reason = 'Stable',''
    elif ma25_wins==0 and ma26_wins>0:
        alert = 'Momentum'
        reason = f"New wins Mar+Apr '26 ({ma26_wins} vs 0)"
    elif ma25_wins>0 and ma26_wins==0:
        alert = 'At Risk'
        reason = f"Mar+Apr '26 down 100% vs Mar+Apr '25 (0 vs {ma25_wins} wins)"
    else:
        chg = (ma26_wins-ma25_wins)/ma25_wins
        pct = abs(round(chg*100))
        d   = 'down' if chg<0 else 'up'
        if chg<=-0.25:   alert='At Risk'
        elif chg<=-0.10: alert='Watch'
        elif chg>=0.15:  alert='Momentum'
        else:            alert='Stable'
        if chg<0:
            reason = f"Mar+Apr '26 down {pct}% vs Mar+Apr '25 ({ma26_wins} vs {ma25_wins} wins)"
        elif chg>0:
            reason = f"Mar+Apr '26 up {pct}% vs Mar+Apr '25 ({ma26_wins} vs {ma25_wins} wins)"
        else:
            reason = f"Mar+Apr '26 +0% vs Mar+Apr '25 ({ma26_wins} vs {ma25_wins} wins)"
    tier = tier_label(t12m)
    if mar26==0 and apr26==0: trend='flat'
    elif apr26>mar26: trend='up'
    elif apr26<mar26: trend='down'
    else: trend='flat'
    pipelines_for_acct = sorted({p for p in grp['Pipeline'].dropna().unique() if p}) if 'Pipeline' in grp.columns else []
    all_marketers = sorted({m for m in grp['Marketer'].dropna().unique() if m})
    accounts.append({'mkt':mkt,'terr':terr,'rep':mk_display,'org':org,'tier':tier,'t12m':t12m,
        'w2026':w2026,'w2025':w2025,'w2024':w2024,'feb26':feb26,'mar26':mar26,'apr26':apr26,
        'trend':trend,'alert':alert,'reason':reason,
        'jf26':ma26_wins,'jf25':ma25_wins,
        'pipelines':pipelines_for_acct,
        'all_marketers':all_marketers})

total_accounts = len(accounts)
alert_counts = {}
for a in accounts:
    if a['alert'] in ('At Risk','Watch','Stable','Momentum'):
        if a['jf26']>0 or a['jf25']>0:
            alert_counts[a['alert']] = alert_counts.get(a['alert'],0)+1

tier_counts = {}
for a in accounts:
    tier_counts[a['tier']] = tier_counts.get(a['tier'],0)+1

print(f"Accounts: {total_accounts}")
print(f"Alert counts (active): {alert_counts}")
print(f"Tier counts: {tier_counts}")
print(f"JF YoY: {jf_yoy_pct}%")

# ── Chart data ────────────────────────────────────────────────────────────────
# Mar+Apr by Market (last 2 complete months) — bar chart stays at market level (6 bars)
jf_by_terr = {}
for t in MARKET_ORDER:
    jf_by_terr[t] = {
        'cur': len(df_won[(df_won['Market']==t)&(df_won['Year']==2026)&(df_won['Month'].isin([3,4]))]),
        'prev': len(df_won[(df_won['Market']==t)&(df_won['Year']==2025)&(df_won['Month'].isin([3,4]))]),
    }

# Rolling 12M deal/win volume vs prior year (per month).
# "Deal" = ALL deals created in that month (matches Pipedrive's deal count view),
# regardless of current Won/Lost/Open status. "Win" = Won status only.
roll_deals_cur, roll_deals_prev = [], []
roll_wins_cur,  roll_wins_prev  = [], []
for ym, ym_prev in zip(ROLL12, ROLL12_PREV):
    roll_deals_cur.append(len(df[df['YM']==ym]))
    roll_deals_prev.append(len(df[df['YM']==ym_prev]))
    roll_wins_cur.append(len(df_won[df_won['YM']==ym]))
    roll_wins_prev.append(len(df_won[df_won['YM']==ym_prev]))

# ROLL12 is May'25..Apr'26, indices 10=Mar'26, 11=Apr'26 (last 2 complete months)
jf26_deals_kpi = roll_deals_cur[10] + roll_deals_cur[11]
jf25_deals_kpi = roll_deals_prev[10] + roll_deals_prev[11]
jf26_wins_kpi  = roll_wins_cur[10] + roll_wins_cur[11]
jf25_wins_kpi  = roll_wins_prev[10] + roll_wins_prev[11]

print(f"\nDeal creation JF26={jf26_deals_kpi} vs JF25={jf25_deals_kpi}")
print(f"Win vol JF26={jf26_wins_kpi} vs JF25={jf25_wins_kpi}")

# ── Per-rep monthly volumes (for filtered volume charts) ────────────────────
# Keyed on (Marketer, Territory, Market) so the territory multi-select filter works
# at fine grain. `dc/dp` = ALL deals created that month (matches Pipedrive view);
# `wc/wp` = Won deals only.
print("Computing per-rep volumes…")
vol_by_rep = {}
for (mk, terr, mkt), _ in df.groupby(['Marketer','Territory','Market']):
    rep_all = df[(df['Marketer']==mk)&(df['Territory']==terr)&(df['Market']==mkt)]
    rep_won = df_won[(df_won['Marketer']==mk)&(df_won['Territory']==terr)&(df_won['Market']==mkt)]
    dc, dp, wc, wp = [], [], [], []
    for ym, ym_prev in zip(ROLL12, ROLL12_PREV):
        dc.append(int(len(rep_all[rep_all['YM']==ym])))
        dp.append(int(len(rep_all[rep_all['YM']==ym_prev])))
        wc.append(int(len(rep_won[rep_won['YM']==ym])))
        wp.append(int(len(rep_won[rep_won['YM']==ym_prev])))
    vol_by_rep[f"{mk}||{terr}||{mkt}"] = {'terr': terr, 'mkt': mkt, 'rep': mk, 'dc': dc, 'dp': dp, 'wc': wc, 'wp': wp}

# ── Weekly deal/win volume — last 12 weeks vs prior year ─────────────────────
print("Computing weekly volumes…")
# ISO week: Monday start, Week 1 contains first Thursday of January
iso = df['Date'].dt.isocalendar()
df['ISOYear'] = iso.year.astype(int)
df['ISOWeek'] = iso.week.astype(int)

iso_won = df_won['Date'].dt.isocalendar()
df_won['ISOYear'] = iso_won.year.astype(int)
df_won['ISOWeek'] = iso_won.week.astype(int)

iso_closed = df_closed['Date'].dt.isocalendar()
df_closed['ISOYear'] = iso_closed.year.astype(int)
df_closed['ISOWeek'] = iso_closed.week.astype(int)

# Find the last complete ISO week (the week before the one containing today)
_today = pd.Timestamp.now()
_iso_today = _today.isocalendar()
_cur_iso_yr, _cur_iso_wk = int(_iso_today[0]), int(_iso_today[1])
# Go back 1 week to get last complete week
_end_dt = _today - pd.Timedelta(days=_today.weekday()) - pd.Timedelta(days=1)  # last Sunday
_end_iso = _end_dt.isocalendar()
_end_iso_yr, _end_iso_wk = int(_end_iso[0]), int(_end_iso[1])

# Build list of last 12 complete weeks as (iso_year, iso_week) tuples
wk_list_cur = []
_d = _end_dt
for _ in range(12):
    _i = _d.isocalendar()
    wk_list_cur.insert(0, (int(_i[0]), int(_i[1])))
    _d -= pd.Timedelta(weeks=1)

# Prior year: same ISO week numbers, one year earlier
wk_list_prev = [(y-1, w) for y, w in wk_list_cur]

# Labels: show the Monday start date of each week
wk_labels = []
for y, w in wk_list_cur:
    wk_labels.append(f"Wk {w}")

# Aggregate totals — "Deal" = all deals created in the week (matches Pipedrive
# "deal count" view). "Win" = Won-status deals only.
wk_deals_cur, wk_deals_prev = [], []
wk_wins_cur, wk_wins_prev = [], []
for (y,w), (yp,wp_) in zip(wk_list_cur, wk_list_prev):
    wk_deals_cur.append(int(len(df[(df['ISOYear']==y)&(df['ISOWeek']==w)])))
    wk_deals_prev.append(int(len(df[(df['ISOYear']==yp)&(df['ISOWeek']==wp_)])))
    wk_wins_cur.append(int(len(df_won[(df_won['ISOYear']==y)&(df_won['ISOWeek']==w)])))
    wk_wins_prev.append(int(len(df_won[(df_won['ISOYear']==yp)&(df_won['ISOWeek']==wp_)])))

# Per-rep weekly volumes for filter support — keyed on (Marketer, Territory, Market).
# wdc/wdp = ALL deals (matches Pipedrive); wwc/wwp = Wins only.
wk_vol_by_rep = {}
for (mk, terr, mkt), _ in df.groupby(['Marketer','Territory','Market']):
    rep_all = df[(df['Marketer']==mk)&(df['Territory']==terr)&(df['Market']==mkt)]
    rep_won = df_won[(df_won['Marketer']==mk)&(df_won['Territory']==terr)&(df_won['Market']==mkt)]
    wdc, wdp, wwc, wwp = [], [], [], []
    for (y,w), (yp,wp_) in zip(wk_list_cur, wk_list_prev):
        wdc.append(int(len(rep_all[(rep_all['ISOYear']==y)&(rep_all['ISOWeek']==w)])))
        wdp.append(int(len(rep_all[(rep_all['ISOYear']==yp)&(rep_all['ISOWeek']==wp_)])))
        wwc.append(int(len(rep_won[(rep_won['ISOYear']==y)&(rep_won['ISOWeek']==w)])))
        wwp.append(int(len(rep_won[(rep_won['ISOYear']==yp)&(rep_won['ISOWeek']==wp_)])))
    wk_vol_by_rep[f"{mk}||{terr}||{mkt}"] = {'terr': terr, 'mkt': mkt, 'rep': mk, 'wdc': wdc, 'wdp': wdp, 'wwc': wwc, 'wwp': wwp}

print(f"Weekly: {len(wk_labels)} weeks, deals cur total={sum(wk_deals_cur)}, prev={sum(wk_deals_prev)}")

# ── Zoom-in weekly view: deals CREATED, full-year by ISO week ────────────────
# Uses df (all deals, by creation date). Shape: 53-element arrays (index = ISO week 1..53).
# UI lets user pick a start/end week to zoom in on a slice. Default = weeks 2..17.
print("Computing zoomable weekly deal-creation series (2025 & 2026, by ISO week)…")
ZOOM_WEEKS = list(range(1, 54))  # 1..53 inclusive (covers all possible ISO weeks)
zoom_wk_labels = [f"Wk {w}" for w in ZOOM_WEEKS]

zoom_deals_2026, zoom_deals_2025 = [], []
for w in ZOOM_WEEKS:
    zoom_deals_2026.append(int(len(df[(df['ISOYear']==2026)&(df['ISOWeek']==w)])))
    zoom_deals_2025.append(int(len(df[(df['ISOYear']==2025)&(df['ISOWeek']==w)])))

# Per-rep weekly counts so the filter bar works
zoom_vol_by_rep = {}
for (mk, terr, mkt), _ in df.groupby(['Marketer','Territory','Market']):
    rep = df[(df['Marketer']==mk)&(df['Territory']==terr)&(df['Market']==mkt)]
    z26, z25 = [], []
    for w in ZOOM_WEEKS:
        z26.append(int(len(rep[(rep['ISOYear']==2026)&(rep['ISOWeek']==w)])))
        z25.append(int(len(rep[(rep['ISOYear']==2025)&(rep['ISOWeek']==w)])))
    zoom_vol_by_rep[f"{mk}||{terr}||{mkt}"] = {'terr': terr, 'mkt': mkt, 'rep': mk, 'z26': z26, 'z25': z25}

_zc = sum(zoom_deals_2026[1:18]); _zp = sum(zoom_deals_2025[1:18])
print(f"Zoom default (Wk 2-17): 2026={_zc} vs 2025={_zp} ({round((_zc/_zp-1)*100) if _zp else 0}% YoY)")

# ── Monthly heatmap data (May'25–Apr'26) ─────────────────────────────────────
print("Computing monthly heatmap…")
heatmap_rows = []
for (mkt,terr,org), grp_all in df_closed.groupby(['Market','Territory','Org']):
    # Only include if there's activity in the rolling window
    in_window = grp_all[grp_all['YM'].isin(ROLL12)]
    if len(in_window) == 0: continue
    # Dominant marketer in window
    _mk_counts = in_window['Marketer'].value_counts()
    if len(_mk_counts) > 0:
        mk = _mk_counts.index[0]
        other_count = max(0, len(_mk_counts) - 1)
        mk_display = f"{mk}" + (f" +{other_count}" if other_count else "")
    else:
        mk_display = 'Unassigned'
    months_d, months_w = [], []
    months_d_prev, months_w_prev = [], []
    has_any = False
    for ym, ym_prev in zip(ROLL12, ROLL12_PREV):
        sub = grp_all[grp_all['YM']==ym]
        d = len(sub)
        w = len(sub[sub['Status']=='Won'])
        months_d.append(d if d>0 else None)
        months_w.append(w if d>0 else None)
        if d>0: has_any=True
        sub_p = grp_all[grp_all['YM']==ym_prev]
        dp = len(sub_p)
        wp = len(sub_p[sub_p['Status']=='Won'])
        months_d_prev.append(dp if dp>0 else None)
        months_w_prev.append(wp if dp>0 else None)
    if not has_any: continue
    total_d = sum(x for x in months_d if x)
    total_w = sum(x for x in months_w if x)
    total_pct = round(total_w/total_d*100) if total_d>0 else None
    heatmap_rows.append({'mkt':mkt,'terr':terr,'rep':mk_display,'org':org,
        'months_d':months_d,'months_w':months_w,
        'months_d_prev':months_d_prev,'months_w_prev':months_w_prev,
        'total_d':total_d,'total_w':total_w,'total_pct':total_pct})

print(f"Heatmap rows: {len(heatmap_rows)}")

# Sort heatmap by Apr '26 win rate desc (last month in window), then total_pct desc
def last_month_wr(r):
    d = r['months_d'][-1]
    w = r['months_w'][-1]
    if d is None or d==0: return -1
    return w/d
heatmap_rows.sort(key=lambda r: (last_month_wr(r), r['total_pct'] or 0), reverse=True)

# ── Territory metadata for the multi-select filter ───────────────────────────
# Build TERRITORY_ORDER from the territories actually present in the data,
# grouped by market in MARKET_ORDER, then alphabetical within each market.
# Unassigned floats to the bottom regardless of market.
_terrs_seen = sorted({a['terr'] for a in accounts}.union({h['terr'] for h in heatmap_rows}))
_terr_groups = {m: [] for m in MARKET_ORDER}
_terr_groups['UNK'] = []  # for Unassigned territory
for t in _terrs_seen:
    if t == 'Unassigned':
        if 'Unassigned' not in _terr_groups['UNK']:
            _terr_groups['UNK'].append('Unassigned')
        continue
    m = terr_to_market.get(t, 'UNK')
    if m in _terr_groups:
        _terr_groups[m].append(t)
    else:
        _terr_groups.setdefault('UNK', []).append(t)
TERRITORY_ORDER = []  # flat list, ordered by market then alpha
for m in MARKET_ORDER:
    TERRITORY_ORDER.extend(sorted(_terr_groups.get(m, [])))
TERRITORY_ORDER.extend(sorted(_terr_groups.get('UNK', [])))
TERRITORY_TO_MARKET = {t: terr_to_market.get(t, 'UNK') for t in TERRITORY_ORDER}
MARKET_LABELS = {  # 3-letter code → human label
    'AUS':'Austin','DAL':'Dallas','HOU':'Houston','PHX':'Phoenix',
    'SA':'San Antonio','UT':'Utah','UNK':'Unassigned',
}

# ── Build JSON payload ────────────────────────────────────────────────────────
payload = {
    'updateDate': UPDATE_DATE,
    'totalAccounts': total_accounts,
    'kpi': {
        't12m': total_t12m, '2025': total_2025, '2024': total_2024,
        'jfYoy': jf_yoy_pct, 'jf26': jf26_total, 'jf25': jf25_total,
        # New "Deals Created" scorecards
        'thisWeek': deals_this_week,
        'lastWeek': deals_last_week,
        'last90': deals_last_90, 'prev90': deals_prev_90, 'pct90': deals_90_yoy_pct,
        'mtd': deals_mtd, 'mtdLY': deals_mtd_ly, 'mtdPct': deals_mtd_pct,
    },
    'alertCounts': alert_counts,
    'tierCounts': tier_counts,
    'jfByTerr': jf_by_terr,
    'roll12Labels': ROLL12_LBL,
    'rollDealsCur': roll_deals_cur, 'rollDealsPrev': roll_deals_prev,
    'rollWinsCur': roll_wins_cur,   'rollWinsPrev': roll_wins_prev,
    'jf26DealKpi': jf26_deals_kpi, 'jf25DealKpi': jf25_deals_kpi,
    'jf26WinKpi': jf26_wins_kpi,   'jf25WinKpi': jf25_wins_kpi,
    'roll12MonthLabels': ROLL12_LBL,
    'accounts': accounts,
    'heatmap': heatmap_rows,
    'volByRep': vol_by_rep,
    'wkLabels': wk_labels,
    'wkDealsCur': wk_deals_cur, 'wkDealsPrev': wk_deals_prev,
    'wkWinsCur': wk_wins_cur,   'wkWinsPrev': wk_wins_prev,
    'wkVolByRep': wk_vol_by_rep,
    'zoomWkLabels': zoom_wk_labels,
    'zoomDeals2026': zoom_deals_2026, 'zoomDeals2025': zoom_deals_2025,
    'zoomVolByRep': zoom_vol_by_rep,
    'territoryOrder': TERRITORY_ORDER,
    'territoryToMarket': TERRITORY_TO_MARKET,
    'marketOrder': MARKET_ORDER,
    'marketLabels': MARKET_LABELS,
}

json_str = json.dumps(payload, ensure_ascii=False)
print(f"JSON size: {len(json_str)//1024} KB")

# ── Write HTML ────────────────────────────────────────────────────────────────
html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Referral Deals Created Pipeline</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;font-size:13px;background:#f0f2f5;color:#222}
/* ─── HEADER ─── */
.hdr{background:linear-gradient(135deg,#0d2645 0%,#163a5f 100%);color:#fff;padding:14px 24px;display:flex;align-items:center;justify-content:space-between}
.hdr-left h1{font-size:18px;font-weight:700;letter-spacing:-.2px}
.hdr-left .sub{font-size:11px;color:#8ab4d4;margin-top:2px}
.hdr-right{text-align:right;font-size:11px;color:#8ab4d4}
.hdr-right .acc{font-size:16px;font-weight:700;color:#fff;display:block}
/* ─── ALERT BANNER ─── */
.banner{background:#fffde7;border-left:4px solid #f9a825;padding:9px 24px;font-size:11.5px;color:#555;display:flex;align-items:center;gap:8px}
.banner b{color:#333}
/* ─── FILTERS ─── */
.filters{background:#fff;border-bottom:1px solid #e2e8f0;padding:10px 24px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.filters .fgrp{display:flex;align-items:center;gap:5px}
.filters label{font-size:10px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
.filters select{font-size:12px;padding:5px 10px;border:1px solid #d1d5db;border-radius:6px;background:#fff;color:#333;cursor:pointer;min-width:130px}
.filters .reset-btn{margin-left:auto;background:#fff;border:1px solid #d1d5db;border-radius:6px;padding:5px 14px;font-size:12px;color:#555;cursor:pointer;transition:background .15s}
.filters .reset-btn:hover{background:#f3f4f6}
.rep-dropdown{position:relative;display:inline-block}
.rep-dd-btn{font-size:12px;padding:5px 10px;border:1px solid #d1d5db;border-radius:6px;background:#fff;color:#333;cursor:pointer;min-width:180px;text-align:left;display:flex;justify-content:space-between;align-items:center;gap:6px}
.rep-dd-btn:hover{background:#f3f4f6}
.rep-dd-menu{position:absolute;top:100%;left:0;z-index:100;background:#fff;border:1px solid #d1d5db;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.12);max-height:320px;overflow-y:auto;min-width:220px;padding:4px 0;margin-top:4px}
.rep-dd-menu label{display:flex;align-items:center;gap:8px;padding:5px 12px;font-size:12px;cursor:pointer;white-space:nowrap;transition:background .1s}
.rep-dd-menu label:hover{background:#f0f4f8}
.rep-dd-menu input[type=checkbox]{accent-color:#1e40af;width:14px;height:14px;cursor:pointer}
.rep-dd-menu .rep-dd-actions{display:flex;gap:6px;padding:6px 10px;border-top:1px solid #e5e7eb;margin-top:2px;position:sticky;bottom:0;background:#fff}
.rep-dd-menu .rep-dd-actions button{flex:1;padding:4px 8px;font-size:11px;border:1px solid #d1d5db;border-radius:4px;background:#fff;cursor:pointer;color:#555}
.rep-dd-menu .rep-dd-actions button:hover{background:#f0f4f8}
.rep-dd-menu .rep-dd-actions button.primary{background:#1e40af;color:#fff;border-color:#1e40af}
.rep-dd-menu .rep-dd-actions button.primary:hover{background:#1e3a8a}
/* ─── LAYOUT ─── */
.content{padding:16px 24px}
.row{display:flex;gap:14px;margin-bottom:14px;flex-wrap:wrap}
/* ─── KPI CARDS ─── */
.kpi-card{flex:1;min-width:180px;background:#fff;border-radius:10px;padding:16px 20px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.kpi-card .label{font-size:10px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px}
.kpi-card .value{font-size:28px;font-weight:700;color:#1a2e4a;line-height:1}
.kpi-card .sub{font-size:11px;color:#888;margin-top:4px}
.kpi-card.neg .value{color:#dc2626}
.kpi-card.pos .value{color:#16a34a}
/* ─── ALERT KPI CARDS ─── */
.alert-card{flex:1;min-width:160px;background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 1px 4px rgba(0,0,0,.07);display:flex;align-items:center;gap:14px}
.alert-dot{width:38px;height:38px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:18px}
.alert-card .info .num{font-size:26px;font-weight:700;line-height:1}
.alert-card .info .lbl{font-size:11px;color:#666;margin-top:2px}
.alert-card .info .sub{font-size:10px;color:#999}
.ac-risk{background:#fff1f0}.ac-risk .alert-dot{background:#fca5a5}.ac-risk .info .num{color:#dc2626}
.ac-watch{background:#fffbeb}.ac-watch .alert-dot{background:#fcd34d}.ac-watch .info .num{color:#d97706}
.ac-stable{background:#eff6ff}.ac-stable .alert-dot{background:#93c5fd}.ac-stable .info .num{color:#2563eb}
.ac-momentum{background:#f0fdf4}.ac-momentum .alert-dot{background:#86efac}.ac-momentum .info .num{color:#16a34a}
/* ─── CHART CARDS ─── */
.chart-card{flex:1;min-width:300px;background:#fff;border-radius:10px;padding:16px 20px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.chart-card .chart-title{font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}
.chart-card .chart-wrap{position:relative}
/* ─── VOLUME CHART CARDS ─── */
.vol-card{flex:1;min-width:320px;background:#fff;border-radius:10px;padding:16px 20px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.vol-card .vol-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:4px}
.vol-card .vol-title{display:flex;align-items:center;gap:6px;font-size:11px;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:.4px}
.vol-card .vol-kpi{text-align:right;font-size:11px;color:#aaa}
.vol-card .vol-kpi .v1{font-size:20px;font-weight:700;color:#1e3a5f}
.vol-card .vol-kpi .v2{font-size:14px;color:#bbb;margin-left:8px}
.vol-card .vol-kpi .chg{font-size:11px;font-weight:600;display:block;margin-top:2px}
.vol-card .vol-kpi .chg.down{color:#dc2626}
.vol-card .vol-kpi .chg.up{color:#16a34a}
.vol-card .vol-sub{font-size:10px;color:#aaa;margin-bottom:10px}
/* ─── MONTHLY HEATMAP ─── */
.section-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.section-hdr h2{font-size:12px;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:.5px}
.section-hdr .acc-count{font-size:11px;color:#aaa}
.toggle-group{display:flex;border:1px solid #d1d5db;border-radius:6px;overflow:hidden}
.toggle-btn{padding:4px 14px;font-size:12px;cursor:pointer;background:#fff;border:none;color:#666;transition:background .15s}
.toggle-btn.active{background:#1e3a5f;color:#fff;font-weight:600}
.heatmap-wrap{overflow-x:auto;max-height:420px;overflow-y:auto;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
#heatmap-tbl{border-collapse:collapse;width:100%;min-width:1100px;background:#fff}
#heatmap-tbl thead th{position:sticky;top:0;background:#fff;z-index:10;font-size:10px;font-weight:700;color:#666;padding:7px 6px;text-align:center;border-bottom:2px solid #e5e7eb;white-space:nowrap;cursor:pointer}
#heatmap-tbl thead th:hover{background:#f3f4f6}
#heatmap-tbl thead th.sorted{color:#1e3a5f}
#heatmap-tbl thead th:nth-child(1),#heatmap-tbl thead th:nth-child(2),#heatmap-tbl thead th:nth-child(3){text-align:left}
#heatmap-tbl tbody td{padding:4px 5px;font-size:11px;border-bottom:1px solid #f0f0f0;white-space:nowrap}
#heatmap-tbl tbody td.cell-terr{font-weight:700;font-size:10px;padding:4px 8px}
#heatmap-tbl tbody td.cell-rep{font-size:11px;color:#555}
#heatmap-tbl tbody td.cell-org{max-width:200px;overflow:hidden;text-overflow:ellipsis;font-size:11px}
#heatmap-tbl tbody td.cell-month{text-align:center;font-size:10px;font-weight:600;min-width:48px;border-radius:3px}
#heatmap-tbl tbody td.cell-total{text-align:center;font-weight:700;font-size:11px;min-width:72px}
.hm-dash{color:#ccc}
.hm-prev{font-size:9px;opacity:.6;font-weight:400}
/* ─── ACCOUNT DETAIL ─── */
.detail-wrap{overflow-x:auto;max-height:480px;overflow-y:auto;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.07);margin-top:14px}
#detail-tbl{border-collapse:collapse;width:100%;min-width:1100px;background:#fff}
#detail-tbl thead th{position:sticky;top:0;background:#fff;z-index:10;font-size:10px;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.4px;padding:9px 10px;text-align:left;border-bottom:2px solid #e5e7eb;cursor:pointer;white-space:nowrap}
#detail-tbl thead th:hover{background:#f3f4f6}
#detail-tbl thead th.sorted{color:#1e3a5f}
#detail-tbl thead th.num{text-align:right}
#detail-tbl tbody tr:hover td{background:#f8fafc!important}
#detail-tbl tbody tr.hidden{display:none}
#detail-tbl tbody td{padding:6px 10px;font-size:12px;border-bottom:1px solid #f0f0f0;vertical-align:middle}
#detail-tbl tbody td.num{text-align:right;font-variant-numeric:tabular-nums}
#detail-tbl tbody td.reason{font-size:11px;color:#888;max-width:280px}
.tier-badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap}
.tier-vip{background:#fef9c3;color:#92400e}
.tier-1{background:#cffafe;color:#155e75}
.tier-2{background:#f1f5f9;color:#475569}
.tier-3{background:#f8f8f8;color:#9ca3af}
.alert-badge{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap}
.ab-risk{background:#fef2f2;color:#991b1b}
.ab-watch{background:#fffbeb;color:#92400e}
.ab-stable{background:#eff6ff;color:#1d4ed8}
.ab-momentum{background:#f0fdf4;color:#15803d}
.trend-up{color:#16a34a;font-weight:700}
.trend-down{color:#dc2626;font-weight:700}
.trend-flat{color:#9ca3af}
.terr-pill{display:inline-block;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700;color:#fff}
.t-AUS{background:#e65100}.t-DAL{background:#7b1fa2}.t-HOU{background:#1b5e20}
.t-PHX{background:#b71c1c}.t-SA{background:#c17900;color:#fff}.t-UT{background:#0d47a1}
.t-UNK{background:#6b7280}
.terr-cell{font-size:11px;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.terr-cell .terr-name{color:#444;font-weight:500}
.terr-grp-hdr{font-weight:700;background:#f3f4f6;padding:5px 12px;font-size:11px;color:#1e3a5f;border-top:1px solid #e5e7eb;display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none}
.terr-grp-hdr:first-child{border-top:none}
.terr-grp-hdr:hover{background:#e5e7eb}
.terr-grp-hdr input[type=checkbox]{accent-color:#1e40af;width:14px;height:14px;cursor:pointer;margin:0}
.terr-dd-menu label.terr-child{padding-left:30px}
/* ─── SORT INDICATORS ─── */
.sort-asc::after{content:" ▲";font-size:9px}
.sort-desc::after{content:" ▼";font-size:9px}
/* ─── HIDE ─── */
.hidden{display:none!important}
</style>
</head>
<body>
""" + f"""
<!-- HEADER -->
<div class="hdr">
  <div class="hdr-left">
    <h1>Referral Deals Created Pipeline</h1>
    <div class="sub">Account Tiering &amp; Opportunity Alerts — All Territories</div>
  </div>
  <div class="hdr-right">
    Updated {UPDATE_DATE}
    <span class="acc" id="acct-count">—</span>
  </div>
</div>

<!-- ALERT BANNER -->
<div class="banner">
  <span>ℹ️</span>
  <span><b>Alert method:</b> Compares Mar+Apr '26 vs Mar+Apr '25 (complete months only — May excluded). Trend arrows show Mar→Apr trajectory within 2026.</span>
</div>

<!-- FILTERS -->
<div class="filters">
  <div class="fgrp"><label>Pipeline</label>
    <select id="f-pipeline"><option value="">All Pipelines</option></select></div>
  <div class="fgrp"><label>Territory</label>
    <div class="rep-dropdown" id="terr-dropdown">
      <button class="rep-dd-btn" id="terr-dd-btn" type="button">All Territories <span>&#9662;</span></button>
      <div class="rep-dd-menu" id="terr-dd-menu" style="display:none;min-width:240px">
        <div id="terr-dd-list"></div>
        <div class="rep-dd-actions">
          <button onclick="terrSelectAll()">All</button>
          <button onclick="terrSelectNone()">None</button>
          <button class="primary" onclick="terrApply()">Apply</button>
        </div>
      </div>
    </div></div>
  <div class="fgrp"><label>Rep</label>
    <div class="rep-dropdown" id="rep-dropdown">
      <button class="rep-dd-btn" id="rep-dd-btn" type="button">All Reps <span>&#9662;</span></button>
      <div class="rep-dd-menu" id="rep-dd-menu" style="display:none">
        <div id="rep-dd-list"></div>
        <div class="rep-dd-actions">
          <button onclick="repSelectAll()">All</button>
          <button onclick="repSelectNone()">None</button>
          <button class="primary" onclick="repApply()">Apply</button>
        </div>
      </div>
    </div></div>
  <div class="fgrp"><label>Alert</label>
    <select id="f-alert"><option value="">All Alerts</option>
      <option>At Risk</option><option>Watch</option>
      <option>Stable</option><option>Momentum</option>
    </select></div>
  <div class="fgrp"><label>Tier</label>
    <select id="f-tier"><option value="">All Tiers</option>
      <option>VIP</option><option>Tier 1</option>
      <option>Tier 2</option><option>Tier 3</option>
    </select></div>
  <div class="fgrp"><label>Trend</label>
    <select id="f-trend"><option value="">All Trends</option>
      <option value="up">↑ Up</option>
      <option value="down">↓ Down</option>
      <option value="flat">→ Flat</option>
    </select></div>
  <button class="reset-btn" onclick="resetFilters()">↺ Reset</button>
</div>

<div class="content">
""" + r"""
  <!-- KPI ROW 1 -->
  <div class="row" id="kpi-row1">
    <div class="kpi-card">
      <div class="label">Deals Created This Week</div>
      <div class="value" id="kpi-this-week">—</div>
      <div class="sub">Current ISO week (Mon–Sun)</div>
    </div>
    <div class="kpi-card">
      <div class="label">Deals Created Last Week</div>
      <div class="value" id="kpi-last-week">—</div>
      <div class="sub">Previous ISO week</div>
    </div>
    <div class="kpi-card" id="kpi-90d-card">
      <div class="label">Last 90 Days vs Prev 90</div>
      <div class="value" id="kpi-90d">—</div>
      <div class="sub" id="kpi-90d-sub">—</div>
    </div>
    <div class="kpi-card" id="kpi-mtd-card">
      <div class="label">MTD vs LY</div>
      <div class="value" id="kpi-mtd">—</div>
      <div class="sub" id="kpi-mtd-sub">—</div>
    </div>
  </div>

  <!-- KPI ROW 2 - ALERTS -->
  <div class="row">
    <div class="alert-card ac-risk">
      <div class="alert-dot">🔴</div>
      <div class="info">
        <div class="num" id="cnt-risk">—</div>
        <div class="lbl">At Risk</div>
        <div class="sub">Down ≥25% YoY</div>
      </div>
    </div>
    <div class="alert-card ac-watch">
      <div class="alert-dot">🟡</div>
      <div class="info">
        <div class="num" id="cnt-watch">—</div>
        <div class="lbl">Watch</div>
        <div class="sub">Down 10–24% YoY</div>
      </div>
    </div>
    <div class="alert-card ac-stable">
      <div class="alert-dot">🔵</div>
      <div class="info">
        <div class="num" id="cnt-stable">—</div>
        <div class="lbl">Stable</div>
        <div class="sub">Within normal range</div>
      </div>
    </div>
    <div class="alert-card ac-momentum">
      <div class="alert-dot">🟢</div>
      <div class="info">
        <div class="num" id="cnt-momentum">—</div>
        <div class="lbl">Momentum</div>
        <div class="sub">Up ≥15% YoY</div>
      </div>
    </div>
  </div>

  <!-- CHARTS ROW -->
  <div class="row">
    <div class="chart-card" style="flex:1.6">
      <div class="chart-title">Mar+Apr Wins by Territory — '26 vs '25</div>
      <div class="chart-wrap" style="height:200px"><canvas id="chart-terr"></canvas></div>
    </div>
    <div class="chart-card" style="flex:1">
      <div class="chart-title">Account Tiers</div>
      <div class="chart-wrap" style="height:200px"><canvas id="chart-tiers"></canvas></div>
    </div>
    <div class="chart-card" style="flex:1">
      <div class="chart-title">Alert Breakdown</div>
      <div class="chart-wrap" style="height:200px"><canvas id="chart-alerts"></canvas></div>
    </div>
  </div>

  <!-- Rolling 12 Months + Weekly volume charts removed per Court May 15 (covered by new scorecards) -->


  <!-- WEEK-RANGE ZOOM: deals created, picker -->
  <div class="row">
    <div class="vol-card" style="grid-column:1 / -1">
      <div class="vol-header">
        <div class="vol-title">🔍 Zoom: Deals Created by ISO Week — pick a range to compare 2026 vs 2025</div>
        <div class="vol-kpi">
          <span><span class="v1" id="zoom-v1">—</span><span class="v2" id="zoom-v2">—</span></span>
          <span class="chg down" id="zoom-chg">—</span>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:10px;font-size:11px;color:#555;margin:4px 0 6px">
        <label>Start Week
          <input type="number" id="zoom-start" min="1" max="53" value="2" style="width:54px;margin-left:4px;padding:2px 4px;font-size:11px">
        </label>
        <label>End Week
          <input type="number" id="zoom-end" min="1" max="53" value="17" style="width:54px;margin-left:4px;padding:2px 4px;font-size:11px">
        </label>
        <button onclick="applyZoomRange()" style="padding:3px 10px;font-size:11px;background:#0d2645;color:#fff;border:none;border-radius:3px;cursor:pointer">Apply</button>
        <button onclick="setZoomPreset(2,17)" style="padding:3px 8px;font-size:11px;background:#e5e7eb;border:none;border-radius:3px;cursor:pointer">Wk 2-17</button>
        <button onclick="setZoomPreset(1,13)" style="padding:3px 8px;font-size:11px;background:#e5e7eb;border:none;border-radius:3px;cursor:pointer">Q1 (1-13)</button>
        <button onclick="setZoomPreset(14,26)" style="padding:3px 8px;font-size:11px;background:#e5e7eb;border:none;border-radius:3px;cursor:pointer">Q2 (14-26)</button>
        <span id="zoom-sub" style="margin-left:auto;color:#666"></span>
      </div>
      <div class="chart-wrap" style="height:220px"><canvas id="chart-zoom"></canvas></div>
    </div>
  </div>

  <!-- MONTHLY HEATMAP -->
  <div class="section-hdr">
    <div>
      <h2>Monthly Account Breakdown</h2>
      <div style="font-size:10px;color:#aaa;margin-top:2px">Rolling 12 months (May '25 – Apr '26) · Click any month header to sort · Color = win rate (green = high %, red = low %)</div>
    </div>
    <div style="display:flex;align-items:center;gap:12px">
      <div class="toggle-group">
        <button class="toggle-btn" onclick="setHmMode('deals',this)">Deals</button>
        <button class="toggle-btn" onclick="setHmMode('wins',this)">Wins</button>
        <button class="toggle-btn active" onclick="setHmMode('pct',this)">Win %</button>
      </div>
      <span class="acc-count" id="hm-count">—</span>
    </div>
  </div>
  <div class="heatmap-wrap">
    <table id="heatmap-tbl">
      <thead id="hm-head"></thead>
      <tbody id="hm-body"></tbody>
    </table>
  </div>

  <!-- ACCOUNT DETAIL -->
  <div class="section-hdr" style="margin-top:20px">
    <h2>Account Detail</h2>
    <span class="acc-count" id="det-count">—</span>
  </div>
  <div class="detail-wrap">
    <table id="detail-tbl">
      <thead>
        <tr>
          <th onclick="sortDetail(0)">Territory</th>
          <th onclick="sortDetail(1)">Rep</th>
          <th onclick="sortDetail(2)">Organization</th>
          <th onclick="sortDetail(3)">Tier</th>
          <th class="num sorted sort-desc" onclick="sortDetail(4)">T12M Wins</th>
          <th class="num" onclick="sortDetail(5)">2026 Wins</th>
          <th class="num" onclick="sortDetail(6)">2025 Wins</th>
          <th class="num" onclick="sortDetail(7)">2024 Wins</th>
          <th class="num" onclick="sortDetail(8)">Mar '26</th>
          <th class="num" onclick="sortDetail(9)">Apr '26</th>
          <th onclick="sortDetail(10)">Trend</th>
          <th onclick="sortDetail(11)">Alert</th>
          <th>Reason</th>
        </tr>
      </thead>
      <tbody id="det-body"></tbody>
    </table>
  </div>

</div><!-- /content -->

<script>
"""

# Embed the JSON data
html += f"const DATA = {json_str};\n"

html += r"""
// ── Helpers ──────────────────────────────────────────────────────────────────
function fmt(n){ return n==null?'':n.toLocaleString(); }
function tierClass(t){
  if(t==='VIP') return 'tier-vip';
  if(t==='Tier 1') return 'tier-1';
  if(t==='Tier 2') return 'tier-2';
  return 'tier-3';
}
function tierEmoji(t){
  if(t==='VIP') return '🏆 VIP';
  if(t==='Tier 1') return '🥇 Tier 1';
  if(t==='Tier 2') return '🥈 Tier 2';
  return '🥉 Tier 3';
}
function alertClass(a){
  if(a==='At Risk') return 'ab-risk';
  if(a==='Watch') return 'ab-watch';
  if(a==='Stable') return 'ab-stable';
  if(a==='Momentum') return 'ab-momentum';
  return '';
}
function alertDot(a){
  if(a==='At Risk') return '● At Risk';
  if(a==='Watch') return '● Watch';
  if(a==='Stable') return '● Stable';
  if(a==='Momentum') return '● Momentum';
  return '—';
}
function trendHtml(t){
  if(t==='up') return '<span class="trend-up">↑</span>';
  if(t==='down') return '<span class="trend-down">↓</span>';
  return '<span class="trend-flat">→</span>';
}
function wmColor(rate){
  // rate 0..1, returns {bg, color}
  if(rate===null) return {bg:'',color:''};
  if(rate>=0.75)  return {bg:'#1b5e20',color:'#fff'};
  if(rate>=0.5)   return {bg:'#388e3c',color:'#fff'};
  if(rate>=0.34)  return {bg:'#ef9a9a',color:'#333'};
  if(rate>0)      return {bg:'#c62828',color:'#fff'};
  return {bg:'#b71c1c',color:'#fff'};
}

// ── Init KPI cards (Deals Created) ──────────────────────────────────────────
document.getElementById('kpi-this-week').textContent = fmt(DATA.kpi.thisWeek);
document.getElementById('kpi-last-week').textContent = fmt(DATA.kpi.lastWeek);

const k90 = document.getElementById('kpi-90d');
k90.textContent = fmt(DATA.kpi.last90);
const pct90 = DATA.kpi.pct90;
document.getElementById('kpi-90d-sub').textContent = `vs ${fmt(DATA.kpi.prev90)} prior 90 (${pct90>=0?'+':''}${pct90.toFixed(1)}%)`;
document.getElementById('kpi-90d-card').classList.add(pct90<0?'neg':'pos');

const kmtd = document.getElementById('kpi-mtd');
kmtd.textContent = fmt(DATA.kpi.mtd);
const pctmtd = DATA.kpi.mtdPct;
document.getElementById('kpi-mtd-sub').textContent = `vs ${fmt(DATA.kpi.mtdLY)} same-period LY (${pctmtd>=0?'+':''}${pctmtd.toFixed(1)}%)`;
document.getElementById('kpi-mtd-card').classList.add(pctmtd<0?'neg':'pos');

document.getElementById('acct-count').textContent = fmt(DATA.totalAccounts)+' accounts';

document.getElementById('cnt-risk').textContent     = fmt(DATA.alertCounts['At Risk']||0);
document.getElementById('cnt-watch').textContent    = fmt(DATA.alertCounts['Watch']||0);
document.getElementById('cnt-stable').textContent   = fmt(DATA.alertCounts['Stable']||0);
document.getElementById('cnt-momentum').textContent = fmt(DATA.alertCounts['Momentum']||0);

// (Volume KPI + win volume charts removed per Court May 15, 2026)

// ── Charts ───────────────────────────────────────────────────────────────────
// Territory bar chart
const terrChart = new Chart(document.getElementById('chart-terr'),{
  type:'bar',
  data:{
    labels:['AUS','DAL','HOU','PHX','SA','UT'],
    datasets:[
      {label:"Mar+Apr '26",data:['AUS','DAL','HOU','PHX','SA','UT'].map(t=>DATA.jfByTerr[t].cur),
       backgroundColor:'#1e40af'},
      {label:"Mar+Apr '25",data:['AUS','DAL','HOU','PHX','SA','UT'].map(t=>DATA.jfByTerr[t].prev),
       backgroundColor:'#bfdbfe'},
    ]
  },
  options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'bottom',labels:{font:{size:11},boxWidth:12}}},
    scales:{x:{grid:{display:false},ticks:{font:{size:11}}},y:{grid:{color:'#f0f0f0'},ticks:{font:{size:10}}}}}
});

// Tiers donut
const tc = DATA.tierCounts;
const tierChart = new Chart(document.getElementById('chart-tiers'),{
  type:'doughnut',
  data:{
    labels:['VIP','Tier 1','Tier 2','Tier 3'],
    datasets:[{data:[tc['VIP']||0,tc['Tier 1']||0,tc['Tier 2']||0,tc['Tier 3']||0],
     backgroundColor:['#f59e0b','#0891b2','#475569','#d1d5db'],borderWidth:2}]
  },
  options:{responsive:true,maintainAspectRatio:false,cutout:'60%',
    plugins:{legend:{position:'bottom',labels:{font:{size:10},boxWidth:10}}}}
});

// Alert donut
const ac = DATA.alertCounts;
const alertChart = new Chart(document.getElementById('chart-alerts'),{
  type:'doughnut',
  data:{
    labels:['At Risk','Watch','Stable','Momentum'],
    datasets:[{data:[ac['At Risk']||0,ac['Watch']||0,ac['Stable']||0,ac['Momentum']||0],
     backgroundColor:['#dc2626','#f97316','#2563eb','#16a34a'],borderWidth:2}]
  },
  options:{responsive:true,maintainAspectRatio:false,cutout:'60%',
    plugins:{legend:{position:'bottom',labels:{font:{size:10},boxWidth:10}}}}
});

// Monthly + weekly deal-creation charts removed per Court May 15, 2026

// Zoom: deals created by ISO week, range picker (default Wk 2-17)
let zoomStart = 2, zoomEnd = 17;
const zoomChart = new Chart(document.getElementById('chart-zoom'),{
  type:'bar',
  data:{
    labels:[],
    datasets:[
      {label:'2026',data:[],backgroundColor:'#1e40af'},
      {label:'2025',data:[],backgroundColor:'#bfdbfe'},
    ]
  },
  options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{position:'bottom',labels:{font:{size:10},boxWidth:12}},
             tooltip:{callbacks:{title:(items)=>items[0].label}}},
    scales:{x:{grid:{display:false},ticks:{font:{size:10}}},
            y:{grid:{color:'#f0f0f0'},ticks:{font:{size:10}}}}}
});

function setZoomPreset(s,e){
  document.getElementById('zoom-start').value = s;
  document.getElementById('zoom-end').value = e;
  applyZoomRange();
}
['zoom-start','zoom-end'].forEach(id=>{
  document.getElementById(id).addEventListener('keydown', ev=>{
    if(ev.key==='Enter') applyZoomRange();
  });
  document.getElementById(id).addEventListener('change', applyZoomRange);
});
function applyZoomRange(){
  let s = parseInt(document.getElementById('zoom-start').value,10);
  let e = parseInt(document.getElementById('zoom-end').value,10);
  if(isNaN(s)||s<1) s=1; if(s>53) s=53;
  if(isNaN(e)||e<1) e=1; if(e>53) e=53;
  if(e<s){ const t=s; s=e; e=t; }
  document.getElementById('zoom-start').value = s;
  document.getElementById('zoom-end').value = e;
  zoomStart = s; zoomEnd = e;
  updateZoomChart();
}
function updateZoomChart(){
  const fv = getFilterValues();
  const s = zoomStart, e = zoomEnd;
  const n = e - s + 1;
  const labels = [];
  for(let w=s; w<=e; w++) labels.push('Wk '+w);
  const cur = Array(n).fill(0), prev = Array(n).fill(0);
  Object.values(DATA.zoomVolByRep).forEach(v=>{
    if(fv.terrs.size>0 && !fv.terrs.has(v.terr)) return;
    if(fv.reps.size>0 && !fv.reps.has(v.rep)) return;
    for(let i=0;i<n;i++){
      const wkIdx = (s + i) - 1;  // arrays are 0-indexed for ISO weeks 1..53
      cur[i]  += v.z26[wkIdx]||0;
      prev[i] += v.z25[wkIdx]||0;
    }
  });
  zoomChart.data.labels = labels;
  zoomChart.data.datasets[0].data = cur;
  zoomChart.data.datasets[1].data = prev;
  zoomChart.update();
  const cSum = cur.reduce((a,b)=>a+b,0);
  const pSum = prev.reduce((a,b)=>a+b,0);
  document.getElementById('zoom-v1').textContent = fmt(cSum);
  document.getElementById('zoom-v2').textContent = ' '+fmt(pSum);
  const pct = pSum>0 ? Math.round((cSum/pSum-1)*100) : 0;
  const el = document.getElementById('zoom-chg');
  el.textContent = (pct>=0?'▲ ':'▼ ')+Math.abs(pct)+'% vs 2025';
  el.className = 'chg '+(pct<0?'down':'up');
  document.getElementById('zoom-sub').textContent =
    'Weeks '+s+'-'+e+' · 2026 vs 2025 (deals created)';
}

// ── Rep multi-select dropdown ─────────────────────────────────────────────────
// Build allReps from each account's all_marketers list (since rep is now a display label like "Brooklyn +2")
const allReps = [...new Set(DATA.accounts.flatMap(a => a.all_marketers || []))].sort();
let selectedReps = new Set();  // empty = all reps
const repDdBtn = document.getElementById('rep-dd-btn');
const repDdMenu = document.getElementById('rep-dd-menu');
const repDdList = document.getElementById('rep-dd-list');

function buildRepList(filter){
  repDdList.innerHTML='';
  const filtered = filter ? allReps.filter(r=>r.toLowerCase().includes(filter.toLowerCase())) : allReps;
  filtered.forEach(r=>{
    const lbl=document.createElement('label');
    const cb=document.createElement('input');
    cb.type='checkbox'; cb.value=r;
    cb.checked = selectedReps.size===0 || selectedReps.has(r);
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(r));
    repDdList.appendChild(lbl);
  });
}
buildRepList();

repDdBtn.addEventListener('click',e=>{
  e.stopPropagation();
  repDdMenu.style.display = repDdMenu.style.display==='none'?'block':'none';
});
document.addEventListener('click',e=>{
  if(!document.getElementById('rep-dropdown').contains(e.target)) repDdMenu.style.display='none';
});

function repSelectAll(){ repDdList.querySelectorAll('input').forEach(cb=>cb.checked=true); }
function repSelectNone(){ repDdList.querySelectorAll('input').forEach(cb=>cb.checked=false); }
function repApply(){
  const checked = [...repDdList.querySelectorAll('input:checked')].map(cb=>cb.value);
  if(checked.length===0 || checked.length===allReps.length){
    selectedReps = new Set();
    repDdBtn.childNodes[0].textContent = 'All Reps ';
  } else {
    selectedReps = new Set(checked);
    repDdBtn.childNodes[0].textContent = checked.length===1 ? checked[0]+' ' : checked.length+' Reps ';
  }
  repDdMenu.style.display='none';
  applyFilters();
}

function getSelectedReps(){ return selectedReps; }

// ── Territory multi-select dropdown (grouped by Market) ──────────────────────
const allTerrs = DATA.territoryOrder;             // ordered list
const terrToMkt = DATA.territoryToMarket;         // territory → 3-letter market code
const marketOrder = DATA.marketOrder;             // [AUS,DAL,HOU,PHX,SA,UT]
const marketLabels = DATA.marketLabels;           // {AUS:"Austin",...,UNK:"Unassigned"}
let selectedTerrs = new Set();                    // empty = all
const terrDdBtn = document.getElementById('terr-dd-btn');
const terrDdMenu = document.getElementById('terr-dd-menu');
const terrDdList = document.getElementById('terr-dd-list');

function buildTerrList(){
  terrDdList.innerHTML = '';
  const groups = {};
  allTerrs.forEach(t=>{
    const m = terrToMkt[t] || 'UNK';
    if(!groups[m]) groups[m] = [];
    groups[m].push(t);
  });
  const order = [...marketOrder, 'UNK'];
  order.forEach(m=>{
    const list = groups[m]; if(!list || list.length===0) return;
    // Group header with toggle-all checkbox
    const hdr = document.createElement('div');
    hdr.className = 'terr-grp-hdr';
    const hcb = document.createElement('input');
    hcb.type='checkbox'; hcb.dataset.market=m;
    hcb.addEventListener('click', e=>{
      e.stopPropagation();
      const checked = hcb.checked;
      list.forEach(t=>{
        const child = terrDdList.querySelector(`input[data-terr="${CSS.escape(t)}"]`);
        if(child) child.checked = checked;
      });
    });
    hdr.appendChild(hcb);
    hdr.appendChild(document.createTextNode(' '+(marketLabels[m]||m)+' ('+list.length+')'));
    hdr.addEventListener('click', e=>{
      if(e.target===hcb) return;
      hcb.checked = !hcb.checked; hcb.dispatchEvent(new Event('click', {bubbles:false}));
    });
    terrDdList.appendChild(hdr);
    list.forEach(t=>{
      const lbl = document.createElement('label');
      lbl.className = 'terr-child';
      const cb = document.createElement('input');
      cb.type='checkbox'; cb.value=t; cb.dataset.terr=t;
      cb.checked = selectedTerrs.size===0 || selectedTerrs.has(t);
      cb.addEventListener('change', ()=>updateTerrGroupHeaderState(m));
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(t));
      terrDdList.appendChild(lbl);
    });
    updateTerrGroupHeaderState(m);
  });
}
function updateTerrGroupHeaderState(m){
  const groupChildren = [...terrDdList.querySelectorAll('input[data-terr]')]
    .filter(cb => (terrToMkt[cb.value]||'UNK')===m);
  if(groupChildren.length===0) return;
  const hdr = terrDdList.querySelector(`input[data-market="${m}"]`);
  if(!hdr) return;
  const checkedCount = groupChildren.filter(cb=>cb.checked).length;
  hdr.checked = checkedCount === groupChildren.length;
  hdr.indeterminate = checkedCount>0 && checkedCount<groupChildren.length;
}
buildTerrList();
terrDdBtn.addEventListener('click', e=>{
  e.stopPropagation();
  terrDdMenu.style.display = terrDdMenu.style.display==='none' ? 'block' : 'none';
});
document.addEventListener('click', e=>{
  if(!document.getElementById('terr-dropdown').contains(e.target)) terrDdMenu.style.display='none';
});
function terrSelectAll(){
  terrDdList.querySelectorAll('input[data-terr]').forEach(cb=>cb.checked=true);
  marketOrder.concat(['UNK']).forEach(updateTerrGroupHeaderState);
}
function terrSelectNone(){
  terrDdList.querySelectorAll('input[data-terr]').forEach(cb=>cb.checked=false);
  marketOrder.concat(['UNK']).forEach(updateTerrGroupHeaderState);
}
function terrApply(){
  const checked = [...terrDdList.querySelectorAll('input[data-terr]:checked')].map(cb=>cb.value);
  if(checked.length===0 || checked.length===allTerrs.length){
    selectedTerrs = new Set();
    terrDdBtn.childNodes[0].textContent = 'All Territories ';
  } else {
    selectedTerrs = new Set(checked);
    if(checked.length===1){
      terrDdBtn.childNodes[0].textContent = checked[0]+' ';
    } else {
      // Show market label if exactly one market's territories all selected
      const mkts = new Set(checked.map(t=>terrToMkt[t]||'UNK'));
      if(mkts.size===1){
        const m = [...mkts][0];
        const allInMkt = allTerrs.filter(t=>(terrToMkt[t]||'UNK')===m).length;
        if(checked.length===allInMkt){
          terrDdBtn.childNodes[0].textContent = (marketLabels[m]||m)+' ';
        } else {
          terrDdBtn.childNodes[0].textContent = checked.length+' Territories ';
        }
      } else {
        terrDdBtn.childNodes[0].textContent = checked.length+' Territories ';
      }
    }
  }
  terrDdMenu.style.display='none';
  applyFilters();
}
function getSelectedTerrs(){ return selectedTerrs; }

function getFilterValues(){
  const terrs=getSelectedTerrs();
  const reps=getSelectedReps();
  const pipeline=document.getElementById('f-pipeline').value;
  const alert=document.getElementById('f-alert').value;
  const tier=document.getElementById('f-tier').value;
  const trend=document.getElementById('f-trend').value;
  return {terrs, reps, pipeline, alert, tier, trend};
}

function matchesFilter(item, fv){
  // item needs .terr, .rep, .alert, .tier, .trend, .pipelines (array)
  if(fv.terrs.size>0 && !fv.terrs.has(item.terr)) return false;
  if(fv.reps.size>0){
    const ms = item.all_marketers || [];
    if (!ms.some(m => fv.reps.has(m))) return false;
  }
  if(fv.pipeline){
    const pls = item.pipelines || [];
    if(!pls.includes(fv.pipeline)) return false;
  }
  if(fv.alert && item.alert!==fv.alert) return false;
  if(fv.tier && item.tier!==fv.tier) return false;
  if(fv.trend && item.trend!==fv.trend) return false;
  return true;
}

// Populate Pipeline dropdown from data
(function initPipelineFilter(){
  const sel = document.getElementById('f-pipeline');
  if(!sel) return;
  const pipes = new Set();
  (DATA.accounts || []).forEach(a => (a.pipelines || []).forEach(p => pipes.add(p)));
  [...pipes].sort().forEach(p => {
    const opt = document.createElement('option');
    opt.value = p; opt.textContent = p;
    sel.appendChild(opt);
  });
})();

function updateCharts(){
  const fv = getFilterValues();
  const isFiltered = fv.terrs.size>0 || fv.reps.size>0 || fv.pipeline || fv.alert || fv.tier || fv.trend;

  // Filter accounts
  const filtAccts = DATA.accounts.filter(a=>matchesFilter(a, fv));
  const activeAccts = filtAccts.filter(a=>a.jf26>0||a.jf25>0);

  // ── KPI scorecards (Deals Created): scorecards show global totals.
  // Per-account filter doesn't easily re-aggregate without per-week per-account data.
  // Keep them at the dataset-wide level (matches HubSpot top-line view).
  document.getElementById('acct-count').textContent = fmt(filtAccts.length)+' accounts';

  // ── Market bar: recalculate from filtered accounts (groups by `mkt`) ──
  const mktLabels = DATA.marketOrder;
  const mktCur  = mktLabels.map(m=>filtAccts.filter(a=>a.mkt===m).reduce((s,a)=>s+(a.jf26||0),0));
  const mktPrev = mktLabels.map(m=>filtAccts.filter(a=>a.mkt===m).reduce((s,a)=>s+(a.jf25||0),0));
  terrChart.data.datasets[0].data = mktCur;
  terrChart.data.datasets[1].data = mktPrev;
  terrChart.update();

  // ── Tier donut: recalculate from filtered accounts ──
  const tiers = {'VIP':0,'Tier 1':0,'Tier 2':0,'Tier 3':0};
  filtAccts.forEach(a=>{tiers[a.tier]=(tiers[a.tier]||0)+1;});
  tierChart.data.datasets[0].data = [tiers['VIP'],tiers['Tier 1'],tiers['Tier 2'],tiers['Tier 3']];
  tierChart.update();

  // ── Alert donut: recalculate from active filtered accounts ──
  const alerts = {'At Risk':0,'Watch':0,'Stable':0,'Momentum':0};
  activeAccts.forEach(a=>{alerts[a.alert]=(alerts[a.alert]||0)+1;});
  alertChart.data.datasets[0].data = [alerts['At Risk'],alerts['Watch'],alerts['Stable'],alerts['Momentum']];
  alertChart.update();

  // (Monthly + weekly volume charts removed; only zoom chart remains)
  updateZoomChart();
}

// ── Heatmap ───────────────────────────────────────────────────────────────────
let hmMode = 'pct';
let hmSortCol = null;
let hmSortDir = 1;

function setHmMode(mode, btn){
  hmMode = mode;
  document.querySelectorAll('.toggle-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  renderHeatmap();
}

function buildHeatmapHead(){
  const head = document.getElementById('hm-head');
  const labels = DATA.roll12MonthLabels;
  head.innerHTML = '';
  const tr = document.createElement('tr');
  ['TERR','REP','ORGANIZATION',...labels,'12MO TOTAL'].forEach((lbl,i)=>{
    const th = document.createElement('th');
    th.textContent = lbl + (i>=3?' ↕':'');
    if(i>=3){ th.onclick=()=>sortHeatmap(i); }
    if(hmSortCol===i) th.classList.add('sorted',hmSortDir===1?'sort-asc':'sort-desc');
    tr.appendChild(th);
  });
  head.appendChild(tr);
}

function renderHeatmap(){
  buildHeatmapHead();
  const body = document.getElementById('hm-body');
  body.innerHTML = '';
  const rows = getFilteredHeatmap();
  document.getElementById('hm-count').textContent = rows.length.toLocaleString()+' accounts';
  rows.forEach(r=>{
    const tr = document.createElement('tr');
    // Market badge + Territory name
    let td = document.createElement('td'); td.className='cell-terr';
    td.innerHTML = renderTerrCell(r.mkt, r.terr);
    tr.appendChild(td);
    // Rep
    td = document.createElement('td'); td.className='cell-rep'; td.textContent=r.rep; tr.appendChild(td);
    // Org
    td = document.createElement('td'); td.className='cell-org'; td.textContent=r.org; td.title=r.org; tr.appendChild(td);
    // Months
    r.months_d.forEach((d,i)=>{
      td = document.createElement('td'); td.className='cell-month';
      const w = r.months_w[i];
      const dp = r.months_d_prev[i];
      const wp = r.months_w_prev[i];
      if(d==null||d===0){ td.innerHTML='<span class="hm-dash">—</span>'; }
      else{
        let val, rate, prevHtml='';
        if(hmMode==='deals'){
          val=d;
          if(dp!=null){
            prevHtml=` <span class="hm-prev">(${dp})</span>`;
            const chg=(d-dp)/dp;
            if(chg>0.20) td.style.background='#dcfce7';
            else if(chg<-0.20) td.style.background='#fee2e2';
          }
          rate=null;
        } else if(hmMode==='wins'){
          val=(w??0);
          if(wp!=null){
            prevHtml=` <span class="hm-prev">(${wp})</span>`;
            const chg=((w??0)-wp)/wp;
            if(chg>0.20) td.style.background='#dcfce7';
            else if(chg<-0.20) td.style.background='#fee2e2';
          }
          rate=null;
        } else{
          rate=d>0?w/d:null;
          val=rate!=null?Math.round(rate*100)+'%':'—';
        }
        if(hmMode==='pct' && rate!=null){
          const c = wmColor(rate);
          td.style.background=c.bg; td.style.color=c.color;
        }
        td.innerHTML = val + prevHtml;
      }
      tr.appendChild(td);
    });
    // Total
    td = document.createElement('td'); td.className='cell-total';
    if(hmMode==='deals') td.textContent=r.total_d||'—';
    else if(hmMode==='wins') td.textContent=r.total_w||'—';
    else{
      const pct = r.total_d>0?Math.round(r.total_w/r.total_d*100):null;
      if(pct!=null){
        const c=wmColor(r.total_d>0?r.total_w/r.total_d:null);
        td.style.background=c.bg; td.style.color=c.color;
        td.textContent=pct+'% ('+r.total_w+'/'+r.total_d+')';
      } else td.textContent='—';
    }
    tr.appendChild(td);
    body.appendChild(tr);
  });
}

function sortHeatmap(col){
  if(hmSortCol===col) hmSortDir*=-1; else{hmSortCol=col;hmSortDir=-1;}
  renderHeatmap();
}

// Helper: render the combined Market badge + Territory name
function renderTerrCell(mkt, terr){
  const m = mkt || 'UNK';
  const t = terr || 'Unassigned';
  // strip leading "Market " prefix from the territory name for display brevity
  // (e.g. "Dallas NE" → "NE", "Houston SE" → "SE", "Utah North" → "North")
  const mLong = (DATA.marketLabels && DATA.marketLabels[m]) || '';
  let suffix = t;
  if(mLong && t.toLowerCase().startsWith(mLong.toLowerCase())){
    suffix = t.slice(mLong.length).trim() || t;
  }
  return `<span class="terr-cell"><span class="terr-pill t-${m}">${m}</span><span class="terr-name">${suffix}</span></span>`;
}

function getFilteredHeatmap(){
  const terrs=getSelectedTerrs();
  const reps=getSelectedReps();
  let rows=DATA.heatmap.filter(r=>
    (terrs.size===0||terrs.has(r.terr))&&(reps.size===0||reps.has(r.rep)));
  if(hmSortCol!==null){
    rows.sort((a,b)=>{
      let va,vb;
      if(hmSortCol===16){ // total
        va=hmMode==='pct'?(a.total_d>0?a.total_w/a.total_d:0):(hmMode==='wins'?a.total_w:a.total_d);
        vb=hmMode==='pct'?(b.total_d>0?b.total_w/b.total_d:0):(hmMode==='wins'?b.total_w:b.total_d);
      } else {
        const mi=hmSortCol-3;
        const da=a.months_d[mi], wa=a.months_w[mi];
        const db=b.months_d[mi], wb=b.months_w[mi];
        va=hmMode==='pct'?(da>0?wa/da:0):(hmMode==='wins'?wa||0:da||0);
        vb=hmMode==='pct'?(db>0?wb/db:0):(hmMode==='wins'?wb||0:db||0);
      }
      return(va-vb)*hmSortDir;
    });
  }
  return rows;
}

renderHeatmap();

// ── Account Detail ─────────────────────────────────────────────────────────────
let detSortCol=4, detSortDir=-1;

function renderDetail(){
  const body=document.getElementById('det-body');
  body.innerHTML='';
  const rows=getFilteredAccounts();
  document.getElementById('det-count').textContent=rows.length.toLocaleString()+' accounts';
  document.getElementById('acct-count').textContent=rows.length.toLocaleString()+' accounts';
  rows.forEach(a=>{
    const tr=document.createElement('tr');
    const cells=[
      renderTerrCell(a.mkt, a.terr),
      a.rep,
      `<span title="${a.org}">${a.org}</span>`,
      `<span class="tier-badge ${tierClass(a.tier)}">${tierEmoji(a.tier)}</span>`,
      a.t12m, a.w2026, a.w2025, a.w2024, a.mar26, a.apr26,
      trendHtml(a.trend),
      a.alert?`<span class="alert-badge ${alertClass(a.alert)}">${alertDot(a.alert)}</span>`:'',
      `<span class="reason">${a.reason||''}</span>`
    ];
    cells.forEach((c,i)=>{
      const td=document.createElement('td');
      if(i>=4&&i<=9) td.className='num';
      if(i===12) td.className='reason';
      td.innerHTML=typeof c==='number'?c:c;
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
  // Update sort indicators
  document.querySelectorAll('#detail-tbl thead th').forEach((th,i)=>{
    th.classList.remove('sort-asc','sort-desc','sorted');
    if(i===detSortCol){th.classList.add('sorted',detSortDir===1?'sort-asc':'sort-desc');}
  });
}

function sortDetail(col){
  if(detSortCol===col) detSortDir*=-1; else{detSortCol=col;detSortDir=-1;}
  renderDetail();
}

function getFilteredAccounts(){
  const terrs=getSelectedTerrs();
  const reps=getSelectedReps();
  const pipeline=document.getElementById('f-pipeline').value;
  const alert=document.getElementById('f-alert').value;
  const tier=document.getElementById('f-tier').value;
  const trend=document.getElementById('f-trend').value;
  let rows=DATA.accounts.filter(a=>
    (terrs.size===0||terrs.has(a.terr))&&
    (reps.size===0|| (a.all_marketers||[]).some(m=>reps.has(m)))&&
    (!pipeline||(a.pipelines||[]).includes(pipeline))&&
    (!alert||a.alert===alert)&&(!tier||a.tier===tier)&&
    (!trend||a.trend===trend));
  const numCols=[4,5,6,7,8,9];
  rows.sort((a,b)=>{
    const keys=['terr','rep','org','tier','t12m','w2026','w2025','w2024','mar26','apr26','trend','alert'];
    let va=a[keys[detSortCol]]??'', vb=b[keys[detSortCol]]??'';
    if(numCols.includes(detSortCol)){va=Number(va)||0;vb=Number(vb)||0;}
    return va<vb?-detSortDir:va>vb?detSortDir:0;
  });
  return rows;
}

function resetFilters(){
  ['f-pipeline','f-alert','f-tier','f-trend'].forEach(id=>{
    const el=document.getElementById(id); if(el) el.value='';
  });
  selectedReps = new Set();
  repDdBtn.childNodes[0].textContent = 'All Reps ';
  buildRepList();
  selectedTerrs = new Set();
  terrDdBtn.childNodes[0].textContent = 'All Territories ';
  buildTerrList();
  applyFilters();
}

function applyFilters(){
  renderDetail();
  renderHeatmap();
  updateCharts();
  // Update KPI counts based on filtered accounts
  const filtered=getFilteredAccounts();
  const active=filtered.filter(a=>a.jf26>0||a.jf25>0);
  const cnts={};
  active.forEach(a=>{cnts[a.alert]=(cnts[a.alert]||0)+1;});
  document.getElementById('cnt-risk').textContent     = (cnts['At Risk']||0).toLocaleString();
  document.getElementById('cnt-watch').textContent    = (cnts['Watch']||0).toLocaleString();
  document.getElementById('cnt-stable').textContent   = (cnts['Stable']||0).toLocaleString();
  document.getElementById('cnt-momentum').textContent = (cnts['Momentum']||0).toLocaleString();
}

['f-pipeline','f-alert','f-tier','f-trend'].forEach(id=>{
  const el=document.getElementById(id); if(el) el.addEventListener('change',applyFilters);
});

renderDetail();
updateZoomChart();
</script>
</body>
</html>
"""

with open(OUTPUT,'w',encoding='utf-8') as f:
    f.write(html)
print(f"Written: {len(html)//1024} KB to {OUTPUT}")
