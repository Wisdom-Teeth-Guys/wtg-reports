#!/usr/bin/env python3
"""
Google Ads Dashboard — per-city, with This Year vs Prior Year comparison.

Top filter: week range (default = current week only, "isolated week").
For each city we show TY vs LY across the selected weeks:
  - Spend  |  Clicks  |  Created (PPC) deals  |  Won (PPC) deals  |  Effective CPA
Plus weekly bar charts overlaying TY vs LY for spend and deals.

No attempt at campaign-level attribution — Pipedrive migration stripped GCLID,
so we work in city-level absolutes.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials


OUT_DIR = Path(__file__).resolve().parent.parent / "out"
OUT_DIR.mkdir(exist_ok=True)
OUTPUT = OUT_DIR / "google_ads_dashboard.html"

CITIES = ["Dallas", "Houston", "San Antonio", "Austin", "Phoenix", "Utah", "Tucson"]

# Finance snapshot: hand-compiled monthly export from the s8e8 "Marketing ROI
# Report" (production/collections are entered from the practice management
# system, not pulled via API). Lives in the "s8e8_finance_raw" tab of the same
# Google Sheet as google_ads_raw/deals_raw -- NOT a file in this repo (raw data
# is never committed here, see .gitignore). Refresh via
# scripts/update_s8e8_snapshot.py whenever Finance shares a new xlsx export.


def load_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SA_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    sh = gspread.authorize(creds).open_by_key(os.environ["GOOGLE_SHEET_ID"])
    ga = pd.DataFrame(sh.worksheet("google_ads_raw").get_all_records())
    deals = pd.DataFrame(sh.worksheet("deals_raw").get_all_records())
    try:
        fin = pd.DataFrame(sh.worksheet("s8e8_finance_raw").get_all_records())
    except gspread.WorksheetNotFound:
        fin = pd.DataFrame(columns=["market", "year", "month_num", "patients", "ad_spend", "collections"])
    return ga, deals, fin


print("Loading sheet…")
ga, deals, fin = load_sheet()
print(f"  → {len(ga)} Google Ads rows, {len(deals)} deals")


def city_from_campaign(name):
    s = (name or "").lower()
    for c in CITIES:
        if re.search(rf"\b{c.lower()}\b", s):
            return c
    return None


# ─── Google Ads side ───
ga["date"] = pd.to_datetime(ga["date"], errors="coerce")
ga = ga.dropna(subset=["date"]).copy()
ga["cost_usd"]     = pd.to_numeric(ga["cost_usd"], errors="coerce").fillna(0.0)
ga["clicks"]       = pd.to_numeric(ga["clicks"], errors="coerce").fillna(0).astype(int)
ga["impressions"]  = pd.to_numeric(ga["impressions"], errors="coerce").fillna(0).astype(int)
ga["city"]         = ga["campaign_name"].apply(city_from_campaign)
ga = ga[ga["city"].notna()].copy()

# ISO year + week per row
iso = ga["date"].dt.isocalendar()
ga["iso_year"] = iso["year"].astype(int)
ga["iso_week"] = iso["week"].astype(int)


# ─── HubSpot deals side ───
_CITY_FOR_PIPELINE = {
    "dallas - wisdom teeth guys":              "Dallas",
    "dallas - wisdom teeth guys pipedrive":    "Dallas",
    "houston - wisdom teeth guys":             "Houston",
    "houston - wisdom teeth guys pipedrive":   "Houston",
    "san antonio - wisdom teeth guys":         "San Antonio",
    "san antonio - wisdom teeth guys pipedrive":"San Antonio",
    "austin - wisdom teeth guys":              "Austin",
    "austin - wisdom teeth guys pipedrive":    "Austin",
    "utah - wisdom teeth guys":                "Utah",
    "utah - wisdom teeth guys pipedrive":      "Utah",
    "phoenix - wisdom teeth guys":             "Phoenix",
    "phoenix - wisdom teeth guys pipedrive":   "Phoenix",
    "tucson - wisdom teeth guys":              "Tucson",
}
deals["city"] = deals["pipeline_name"].fillna("").astype(str).str.strip().str.lower().map(_CITY_FOR_PIPELINE)
deals = deals[deals["city"].notna()].copy()
deals["create_dt"] = pd.to_datetime(deals["create_date"], errors="coerce", utc=True).dt.tz_localize(None)
deals["won_dt"]    = pd.to_datetime(deals["won_time"],    errors="coerce", utc=True).dt.tz_localize(None)
deals["lead_source"] = deals["primary_lead_source"].fillna("").astype(str).str.strip().str.lower()
ads_deals = deals[deals["lead_source"] == "google adwords ppc"].copy()

# ISO year/week for create_dt and won_dt
for col in ("create_dt", "won_dt"):
    valid = ads_deals[col].notna()
    iso = ads_deals.loc[valid, col].dt.isocalendar()
    ads_deals.loc[valid, f"{col}_iy"] = iso["year"].astype(int).values
    ads_deals.loc[valid, f"{col}_iw"] = iso["week"].astype(int).values


# ─── Aggregate by (city, iso_year, iso_week) ───
spend = ga.groupby(["city","iso_year","iso_week"])["cost_usd"].sum().to_dict()
clicks = ga.groupby(["city","iso_year","iso_week"])["clicks"].sum().to_dict()
impr   = ga.groupby(["city","iso_year","iso_week"])["impressions"].sum().to_dict()

created = ads_deals.dropna(subset=["create_dt_iy"]).groupby(
    ["city","create_dt_iy","create_dt_iw"]
).size().to_dict()
won = ads_deals.dropna(subset=["won_dt_iy"]).groupby(
    ["city","won_dt_iy","won_dt_iw"]
).size().to_dict()


# ─── Identify the years we have data for ───
ga_years = sorted(ga["iso_year"].unique().tolist())
hubspot_years = sorted(set(ads_deals["create_dt_iy"].dropna().astype(int).unique()) | set(ads_deals["won_dt_iy"].dropna().astype(int).unique()))
print(f"  Google Ads years: {ga_years}")
print(f"  HubSpot ad-deal years: {hubspot_years}")

# All weeks present in either dataset (1-53)
all_weeks = sorted(set(ga["iso_week"].unique().tolist()) | set(range(1, 54)))


# Build cell lookup: (city, year, week) → metrics
def make_series(d):
    """Return dict {city: {year: {week: value}}}."""
    out = {c: {} for c in CITIES}
    for (city, y, w), v in d.items():
        if city not in out: continue
        try:
            y_int = int(y); w_int = int(w)
        except (TypeError, ValueError): continue
        out[city].setdefault(y_int, {})[w_int] = v
    return out

spend_s   = make_series(spend)
clicks_s  = make_series(clicks)
impr_s    = make_series(impr)
created_s = make_series(created)
won_s     = make_series(won)


# ─── Monthly CRM aggregation (for the finance comparison, which is monthly) ───
ga["cal_year"]  = ga["date"].dt.year
ga["cal_month"] = ga["date"].dt.month
for col in ("create_dt", "won_dt"):
    valid = ads_deals[col].notna()
    ads_deals.loc[valid, f"{col}_cy"] = ads_deals.loc[valid, col].dt.year.values
    ads_deals.loc[valid, f"{col}_cm"] = ads_deals.loc[valid, col].dt.month.values

crm_spend_m   = ga.groupby(["city","cal_year","cal_month"])["cost_usd"].sum().to_dict()
crm_created_m = ads_deals.dropna(subset=["create_dt_cy"]).groupby(
    ["city","create_dt_cy","create_dt_cm"]
).size().to_dict()
crm_won_m = ads_deals.dropna(subset=["won_dt_cy"]).groupby(
    ["city","won_dt_cy","won_dt_cm"]
).size().to_dict()

def make_month_series(d):
    """Return dict {city: {"YYYY-MM": value}}."""
    out = {c: {} for c in CITIES}
    for (city, y, m), v in d.items():
        if city not in out: continue
        try:
            key = f"{int(y):04d}-{int(m):02d}"
        except (TypeError, ValueError): continue
        out[city][key] = v
    return out

crm_spend_ms   = make_month_series(crm_spend_m)
crm_created_ms = make_month_series(crm_created_m)
crm_won_ms     = make_month_series(crm_won_m)


# ─── Finance snapshot (s8e8 report, manually updated -- see "s8e8_finance_raw" comment above) ───
finance_ms = {c: {} for c in CITIES}
finance_as_of = None
if not fin.empty:
    fin = fin[fin["market"].isin(CITIES)].copy()
    for _, r in fin.iterrows():
        try:
            key = f"{int(r['year']):04d}-{int(r['month_num']):02d}"
        except (TypeError, ValueError):
            continue
        def _num(v):
            return None if v in (None, "", "None") or pd.isna(v) else float(v)
        finance_ms[r["market"]][key] = {
            "patients":    _num(r["patients"]),
            "spend":       _num(r["ad_spend"]),
            "collections": _num(r["collections"]),
        }
        finance_as_of = key if finance_as_of is None else max(finance_as_of, key)
    print(f"  Finance snapshot: {len(fin)} rows, latest month {finance_as_of}")
else:
    print("  ! s8e8_finance_raw tab is empty/missing -- comparison table will show no data until it's populated")


UPDATE_DATE = datetime.now(timezone.utc).strftime("%B %-d, %Y")
DATA = {
    "updateDate":    UPDATE_DATE,
    "cities":        CITIES,
    "gaYears":       ga_years,
    "deals": {
        "spend":   spend_s,
        "clicks":  clicks_s,
        "impr":    impr_s,
        "created": created_s,
        "won":     won_s,
    },
    "monthly": {
        "crmSpend":   crm_spend_ms,
        "crmCreated": crm_created_ms,
        "crmWon":     crm_won_ms,
        "finance":    finance_ms,
        "financeAsOf": finance_as_of,
    },
}


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Google Ads Dashboard · YoY</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #f6f7f9; color: #1a2e4a; padding-bottom: 40px; }
.header { background: #fff; border-bottom: 1px solid #e2e8f0; padding: 16px 28px; display: flex; justify-content: space-between; align-items: center; }
.header h1 { font-size: 20px; color: #1e3a5f; }
.header .meta { font-size: 12px; color: #6b7280; }
.back { color: #1e3a5f; text-decoration: none; font-size: 13px; margin-right: 16px; }

.banner { background: #fffbeb; border-bottom: 1px solid #fde68a; padding: 10px 28px; font-size: 12px; color: #92400e; }
.banner b { font-weight: 700; }

.filters { background: #fff; border-bottom: 1px solid #e2e8f0; padding: 14px 28px; display: flex; gap: 16px; align-items: center; flex-wrap: wrap; position: sticky; top: 0; z-index: 10; }
.fg { display: flex; align-items: center; gap: 8px; }
.fg label { font-size: 11px; font-weight: 700; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; }
.wk-input { width: 60px; padding: 5px 8px; font-size: 13px; border: 1px solid #d1d5db; border-radius: 6px; }
.year-select { padding: 5px 8px; font-size: 13px; border: 1px solid #d1d5db; border-radius: 6px; }
.apply-btn { font-size: 12px; padding: 6px 14px; border: 1px solid #1e3a5f; background: #1e3a5f; color: #fff; border-radius: 6px; cursor: pointer; }
.preset-btn { font-size: 11px; padding: 5px 10px; border: 1px solid #d1d5db; background: #fff; border-radius: 6px; cursor: pointer; color: #4b5563; }
.preset-btn:hover { background: #f3f4f6; }

.summary { padding: 20px 28px; display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; max-width: 1800px; margin: 0 auto; }
.kpi { background: #fff; border-radius: 10px; padding: 14px 18px; box-shadow: 0 1px 4px rgba(0,0,0,0.05); }
.kpi .label { font-size: 10px; font-weight: 700; color: #888; text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 6px; }
.kpi .value { font-size: 22px; font-weight: 700; color: #1a2e4a; }
.kpi .sub { font-size: 11px; color: #6b7280; margin-top: 4px; }
.kpi .sub .delta.up { color: #16a34a; font-weight: 600; }
.kpi .sub .delta.down { color: #dc2626; font-weight: 600; }

.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; padding: 0 28px 24px; max-width: 1800px; margin: 0 auto; }
@media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } }
.panel { background: #fff; border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.05); }
.panel h3 { font-size: 16px; color: #1e3a5f; margin-bottom: 8px; display: flex; align-items: center; gap: 10px; }
.panel h3 .city-tag { background: #e0f2fe; color: #075985; font-size: 11px; padding: 2px 8px; border-radius: 12px; font-weight: 600; }
.metrics-table { width: 100%; font-size: 12px; border-collapse: collapse; margin-bottom: 10px; }
.metrics-table th, .metrics-table td { padding: 6px 8px; text-align: right; border-bottom: 1px solid #f1f5f9; }
.metrics-table th { font-size: 10px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.4px; text-align: right; }
.metrics-table th:first-child, .metrics-table td:first-child { text-align: left; font-weight: 600; color: #1e3a5f; }
.delta.up { color: #16a34a; font-weight: 600; }
.delta.down { color: #dc2626; font-weight: 600; }
.chart-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 6px; }
.chart-block .chart-label { font-size: 11px; font-weight: 700; color: #6b7280; text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 4px; }
.chart-block .chart-wrap { height: 140px; }

.reality { padding: 8px 28px 24px; max-width: 1800px; margin: 0 auto; }
.reality-head { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
.reality-head h2 { font-size: 18px; color: #1e3a5f; }
.reality-panel { background: #fff; border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.05); overflow-x: auto; }
.reality-table { width: 100%; font-size: 12px; border-collapse: collapse; min-width: 980px; }
.reality-table th, .reality-table td { padding: 7px 10px; text-align: right; border-bottom: 1px solid #f1f5f9; white-space: nowrap; }
.reality-table th { font-size: 10px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.4px; }
.reality-table th:first-child, .reality-table td:first-child { text-align: left; font-weight: 600; color: #1e3a5f; }
.reality-table thead tr.group-row th { text-align: center; border-bottom: 1px solid #e2e8f0; padding-bottom: 4px; }
.reality-table thead tr.group-row th.crm { color: #1e40af; }
.reality-table thead tr.group-row th.fin { color: #92400e; }
.reality-table tbody tr:hover { background: #f8fafc; }
.reality-table td.na { color: #cbd5e1; }
.reality-table td.pending { color: #cbd5e1; font-style: italic; }
footer { text-align: center; font-size: 11px; color: #9ca3af; padding: 20px; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>💰 Google Ads Dashboard · YoY</h1>
    <div class="meta">Spend, clicks, and HubSpot PPC deals — This Year vs Prior Year by week range</div>
  </div>
  <div>
    <a class="back" href="index.html">← All reports</a>
    <span class="meta">Last build <span id="update-date">—</span></span>
  </div>
</div>

<div class="banner">
  <b>Attribution note:</b> Created/Won counts are HubSpot deals where Primary Lead Source = "Google Adwords PPC".
  Pipedrive migration stripped GCLID so we can't tie spend to specific deals yet — CPA shown is city-level (total spend ÷ total won).
</div>

<div class="filters">
  <div class="fg">
    <label>This Year</label>
    <select class="year-select" id="ty"></select>
  </div>
  <div class="fg">
    <label>Prior Year</label>
    <select class="year-select" id="ly"></select>
  </div>
  <div class="fg">
    <label>Week Range</label>
    <input class="wk-input" type="number" id="wk-start" min="1" max="53" value="20">
    <span style="color:#9ca3af">→</span>
    <input class="wk-input" type="number" id="wk-end" min="1" max="53" value="20">
    <button class="apply-btn" onclick="refresh()">Apply</button>
  </div>
  <div class="fg">
    <button class="preset-btn" onclick="setRange(currentWeek(), currentWeek())">This Week</button>
    <button class="preset-btn" onclick="setRange(currentWeek()-1, currentWeek()-1)">Last Week</button>
    <button class="preset-btn" onclick="setRange(currentWeek()-3, currentWeek())">Last 4 Wks</button>
    <button class="preset-btn" onclick="setRange(currentWeek()-12, currentWeek())">Last 13 Wks</button>
    <button class="preset-btn" onclick="setRange(1, currentWeek())">YTD</button>
  </div>
</div>

<div class="summary" id="summary"></div>
<div class="grid" id="grid"></div>

<div class="reality">
  <div class="reality-head">
    <h2>Reality Check: CRM Funnel vs. Finance Actuals</h2>
    <div class="fg">
      <label style="font-size:11px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;">City</label>
      <select class="year-select" id="reality-city"></select>
    </div>
  </div>
  <div class="banner" style="border-radius:8px;margin-bottom:12px;">
    <b>Two different numbers, by design:</b>
    <span style="color:#1e40af;font-weight:600;">CRM columns</span> = live daily sync from HubSpot/Pipedrive: raw Google Ads spend and deals marked Won (a pipeline stage, not a completed surgery).
    <span style="color:#92400e;font-weight:600;">Finance columns</span> = the s8e8 Marketing ROI Report, a hand-compiled monthly snapshot from the practice management system: spend including Adwords management fee, and patients tied to a completed surgery, plus collections. Finance data is <b>not live</b> — it's only as current as the last export Finance shared (<span id="reality-as-of">—</span>) and must be refreshed manually via <code>scripts/update_s8e8_snapshot.py</code>.
  </div>
  <div class="reality-panel">
    <table class="reality-table">
      <thead>
        <tr class="group-row"><th></th><th colspan="3" class="crm">CRM (HubSpot/Pipedrive, live)</th><th colspan="4" class="fin">Finance (s8e8 report, manual)</th></tr>
        <tr><th>Month</th><th>Spend</th><th>Won</th><th>CPA</th><th>Spend (w/ mgmt fee)</th><th>Patients (surgery)</th><th>CPA</th><th>Collections</th></tr>
      </thead>
      <tbody id="reality-body"></tbody>
    </table>
  </div>
</div>

<footer>Auto-updated daily · Google Ads (2-yr history) + HubSpot deals where Lead Source = Google Adwords PPC</footer>

<script>
const DATA = __DATA_JSON__;
document.getElementById('update-date').textContent = DATA.updateDate;

function currentWeek(){
  const d = new Date();
  const target = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const dayNum = target.getUTCDay() || 7;
  target.setUTCDate(target.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(target.getUTCFullYear(), 0, 1));
  return Math.ceil((((target - yearStart) / 86400000) + 1) / 7);
}
function currentYear(){
  const d = new Date();
  const target = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const dayNum = target.getUTCDay() || 7;
  target.setUTCDate(target.getUTCDate() + 4 - dayNum);
  return target.getUTCFullYear();
}

const fmt = n => (n || 0).toLocaleString();
const fmt$ = n => '$' + (Math.round((n||0)*100)/100).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
const pctDelta = (cur, prev) => prev > 0 ? Math.round((cur/prev - 1)*100) : null;

// Populate year selectors
const years = DATA.gaYears.length ? DATA.gaYears : [currentYear() - 1, currentYear()];
years.sort();
const tySel = document.getElementById('ty');
const lySel = document.getElementById('ly');
years.forEach(y => {
  tySel.innerHTML += `<option value="${y}">${y}</option>`;
  lySel.innerHTML += `<option value="${y}">${y}</option>`;
});
tySel.value = currentYear();
lySel.value = Math.max(...years.filter(y => y < currentYear()), years[0]);

// Default week range: current week, both endpoints
const cw = currentWeek();
document.getElementById('wk-start').value = cw;
document.getElementById('wk-end').value   = cw;

function setRange(s, e){
  document.getElementById('wk-start').value = Math.max(1, s);
  document.getElementById('wk-end').value   = Math.max(s, e);
  refresh();
}

function sumWeeks(seriesByYear, year, weeks){
  let total = 0;
  const yearMap = (seriesByYear || {})[year] || {};
  for (const w of weeks) total += (yearMap[w] || 0);
  return total;
}

function buildKpi(label, cur, prev, isCurrency){
  const formatted = isCurrency ? fmt$(cur) : fmt(cur);
  const pd = pctDelta(cur, prev);
  let delta = '';
  if (pd !== null) {
    const sign = pd >= 0 ? '▲' : '▼';
    const cls  = pd >= 0 ? 'up' : 'down';
    delta = `<span class="delta ${cls}">${sign} ${Math.abs(pd)}%</span>`;
  } else if (prev === 0 && cur > 0) {
    delta = `<span class="delta up">▲ new</span>`;
  }
  const prevFmt = isCurrency ? fmt$(prev) : fmt(prev);
  return `<div class="kpi"><div class="label">${label}</div><div class="value">${formatted}</div>
    <div class="sub">vs ${prevFmt} ${delta}</div></div>`;
}

function refresh(){
  const ty = parseInt(tySel.value);
  const ly = parseInt(lySel.value);
  const s  = parseInt(document.getElementById('wk-start').value) || 1;
  const e  = parseInt(document.getElementById('wk-end').value)   || cw;
  const weeks = [];
  for (let w = s; w <= e; w++) weeks.push(w);

  // Overall totals
  let totalSpendCur = 0, totalSpendPrev = 0;
  let totalClicksCur = 0, totalClicksPrev = 0;
  let totalCreatedCur = 0, totalCreatedPrev = 0;
  let totalWonCur = 0, totalWonPrev = 0;
  DATA.cities.forEach(c => {
    totalSpendCur   += sumWeeks(DATA.deals.spend[c],   ty, weeks);
    totalSpendPrev  += sumWeeks(DATA.deals.spend[c],   ly, weeks);
    totalClicksCur  += sumWeeks(DATA.deals.clicks[c],  ty, weeks);
    totalClicksPrev += sumWeeks(DATA.deals.clicks[c],  ly, weeks);
    totalCreatedCur  += sumWeeks(DATA.deals.created[c], ty, weeks);
    totalCreatedPrev += sumWeeks(DATA.deals.created[c], ly, weeks);
    totalWonCur     += sumWeeks(DATA.deals.won[c],     ty, weeks);
    totalWonPrev    += sumWeeks(DATA.deals.won[c],     ly, weeks);
  });
  const cpaCur  = totalWonCur  > 0 ? totalSpendCur  / totalWonCur  : null;
  const cpaPrev = totalWonPrev > 0 ? totalSpendPrev / totalWonPrev : null;

  document.getElementById('summary').innerHTML = `
    ${buildKpi('Total Spend',   totalSpendCur,   totalSpendPrev,   true)}
    ${buildKpi('Total Clicks',  totalClicksCur,  totalClicksPrev,  false)}
    ${buildKpi('Created (PPC)', totalCreatedCur, totalCreatedPrev, false)}
    ${buildKpi('Won (PPC)',     totalWonCur,     totalWonPrev,     false)}
    ${buildKpi('Effective CPA', cpaCur || 0,     cpaPrev || 0,     true)}
  `;
  document.title = `Google Ads · ${ty} vs ${ly} · Wk ${s}${s!==e?'–'+e:''}`;

  // Per-city panels
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  DATA.cities.forEach(city => {
    const tySpend   = sumWeeks(DATA.deals.spend[city],   ty, weeks);
    const lySpend   = sumWeeks(DATA.deals.spend[city],   ly, weeks);
    const tyClicks  = sumWeeks(DATA.deals.clicks[city],  ty, weeks);
    const lyClicks  = sumWeeks(DATA.deals.clicks[city],  ly, weeks);
    const tyCreated = sumWeeks(DATA.deals.created[city], ty, weeks);
    const lyCreated = sumWeeks(DATA.deals.created[city], ly, weeks);
    const tyWon     = sumWeeks(DATA.deals.won[city],     ty, weeks);
    const lyWon     = sumWeeks(DATA.deals.won[city],     ly, weeks);
    const tyCpa = tyWon > 0 ? tySpend / tyWon : null;
    const lyCpa = lyWon > 0 ? lySpend / lyWon : null;

    const safe = city.replace(/\\s+/g,'-').toLowerCase();
    const panel = document.createElement('div'); panel.className = 'panel';
    const deltaCell = (cur, prev) => {
      const pd = pctDelta(cur, prev);
      if (pd === null) return '<td>—</td>';
      const cls = pd >= 0 ? 'up' : 'down';
      const sign = pd >= 0 ? '▲' : '▼';
      return `<td class="delta ${cls}">${sign} ${Math.abs(pd)}%</td>`;
    };

    // Finance (s8e8) mini-table: last 3 reported calendar months, independent
    // of the week-range filter above (finance data is monthly, not weekly).
    const finForCity = DATA.monthly.finance[city] || {};
    const finKeys = Object.keys(finForCity).sort().reverse().slice(0, 3);
    const finRowsHtml = finKeys.length ? finKeys.map(key => {
      const f = finForCity[key];
      const cpa = (f.spend != null && f.patients) ? f.spend / f.patients : null;
      return `<tr>
        <td>${monthLabel(key)}</td>
        <td>${f.spend != null ? fmt$(f.spend) : '—'}</td>
        <td>${f.patients != null ? fmt(f.patients) : '—'}</td>
        <td>${cpa != null ? fmt$(cpa) : '—'}</td>
        <td>${f.collections != null ? fmt$(f.collections) : '—'}</td>
      </tr>`;
    }).join('') : `<tr><td colspan="5" style="text-align:center;color:#9ca3af;">No finance snapshot for ${city}</td></tr>`;

    panel.innerHTML = `
      <h3>${city} <span class="city-tag">${city.toUpperCase()}</span></h3>
      <table class="metrics-table">
        <thead><tr><th></th><th>${ty}</th><th>${ly}</th><th>Δ</th></tr></thead>
        <tbody>
          <tr><td>Spend</td><td>${fmt$(tySpend)}</td><td>${fmt$(lySpend)}</td>${deltaCell(tySpend, lySpend)}</tr>
          <tr><td>Clicks</td><td>${fmt(tyClicks)}</td><td>${fmt(lyClicks)}</td>${deltaCell(tyClicks, lyClicks)}</tr>
          <tr><td>Created (PPC)</td><td>${fmt(tyCreated)}</td><td>${fmt(lyCreated)}</td>${deltaCell(tyCreated, lyCreated)}</tr>
          <tr><td>Won (PPC)</td><td>${fmt(tyWon)}</td><td>${fmt(lyWon)}</td>${deltaCell(tyWon, lyWon)}</tr>
          <tr><td>Effective CPA</td><td>${tyCpa ? fmt$(tyCpa) : '—'}</td><td>${lyCpa ? fmt$(lyCpa) : '—'}</td>${deltaCell(tyCpa || 0, lyCpa || 0)}</tr>
        </tbody>
      </table>
      <div class="chart-row">
        <div class="chart-block"><div class="chart-label">Weekly Spend</div><div class="chart-wrap"><canvas id="${safe}-spend"></canvas></div></div>
        <div class="chart-block"><div class="chart-label">Weekly Won (PPC)</div><div class="chart-wrap"><canvas id="${safe}-won"></canvas></div></div>
      </div>
      <div class="chart-label" style="margin-top:12px;color:#92400e;">Finance (s8e8 report, last 3 months — manual, not tied to week filter)</div>
      <table class="metrics-table">
        <thead><tr><th>Month</th><th>Spend (w/ fee)</th><th>Patients</th><th>CPA</th><th>Collections</th></tr></thead>
        <tbody>${finRowsHtml}</tbody>
      </table>`;
    grid.appendChild(panel);

    // Charts: weekly bars, TY vs LY
    const labels = weeks.map(w => `W${w}`);
    const tySpendBars = weeks.map(w => (DATA.deals.spend[city]?.[ty]?.[w]) || 0);
    const lySpendBars = weeks.map(w => (DATA.deals.spend[city]?.[ly]?.[w]) || 0);
    const tyWonBars   = weeks.map(w => (DATA.deals.won[city]?.[ty]?.[w]) || 0);
    const lyWonBars   = weeks.map(w => (DATA.deals.won[city]?.[ly]?.[w]) || 0);

    new Chart(document.getElementById(`${safe}-spend`), {
      type: 'bar',
      data: { labels, datasets: [
        { label: ty, data: tySpendBars, backgroundColor: '#1e40af' },
        { label: ly, data: lySpendBars, backgroundColor: '#1e40af40' },
      ]},
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom', labels: { font: { size: 9 }, boxWidth: 10 } } },
        scales: { x: { grid: { display: false }, ticks: { font: { size: 9 } } },
                  y: { grid: { color: '#f0f0f0' }, ticks: { font: { size: 9 }, callback: v => '$' + v } } } }
    });
    new Chart(document.getElementById(`${safe}-won`), {
      type: 'bar',
      data: { labels, datasets: [
        { label: ty, data: tyWonBars, backgroundColor: '#16a34a' },
        { label: ly, data: lyWonBars, backgroundColor: '#16a34a40' },
      ]},
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom', labels: { font: { size: 9 }, boxWidth: 10 } } },
        scales: { x: { grid: { display: false }, ticks: { font: { size: 9 } } },
                  y: { grid: { color: '#f0f0f0' }, ticks: { font: { size: 9 } } } } }
    });
  });
}

// ─── Reality Check: monthly CRM vs Finance comparison ───
const MONTH_ABBR = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function monthLabel(key){
  const [y, m] = key.split('-');
  return `${MONTH_ABBR[parseInt(m)]} ${y}`;
}

const realityCitySel = document.getElementById('reality-city');
DATA.cities.forEach(c => { realityCitySel.innerHTML += `<option value="${c}">${c}</option>`; });
realityCitySel.value = DATA.cities[0];
document.getElementById('reality-as-of').textContent = DATA.monthly.financeAsOf ? monthLabel(DATA.monthly.financeAsOf) : 'no data';
realityCitySel.addEventListener('change', renderReality);

function renderReality(){
  const city = realityCitySel.value;
  const m = DATA.monthly;
  const crmSpend   = m.crmSpend[city]   || {};
  const crmCreated = m.crmCreated[city] || {};
  const crmWon     = m.crmWon[city]     || {};
  const finance    = m.finance[city]    || {};

  const keys = new Set([...Object.keys(crmSpend), ...Object.keys(finance)]);
  const sortedKeys = [...keys].sort().reverse().slice(0, 18); // most recent 18 months

  const naCell = () => '<td class="na">—</td>';
  const rows = sortedKeys.map(key => {
    const cSpend = crmSpend[key] || 0;
    const cWon   = crmWon[key]   || 0;
    const cCpa   = cWon > 0 ? cSpend / cWon : null;
    const fin    = finance[key];

    let finCells;
    if (fin) {
      const fSpend = fin.spend;
      const fPat   = fin.patients;
      const fColl  = fin.collections;
      const fCpa   = (fSpend != null && fPat) ? fSpend / fPat : null;
      finCells = `
        <td>${fSpend != null ? fmt$(fSpend) : '—'}</td>
        <td>${fPat != null ? fmt(fPat) : '—'}</td>
        <td>${fCpa != null ? fmt$(fCpa) : '—'}</td>
        <td>${fColl != null ? fmt$(fColl) : '—'}</td>`;
    } else {
      finCells = `<td class="pending" colspan="4">not yet in snapshot</td>`;
    }

    return `<tr>
      <td>${monthLabel(key)}</td>
      <td>${fmt$(cSpend)}</td>
      <td>${fmt(cWon)}</td>
      <td>${cCpa != null ? fmt$(cCpa) : '—'}</td>
      ${finCells}
    </tr>`;
  });

  document.getElementById('reality-body').innerHTML = rows.join('') ||
    '<tr><td colspan="8" style="text-align:center;color:#9ca3af;">No data for this city yet.</td></tr>';
}

renderReality();
refresh();
</script>
</body>
</html>
"""

html = HTML.replace("__DATA_JSON__", json.dumps(DATA, default=str))
OUTPUT.write_text(html, encoding="utf-8")
print(f"  ✓ Written: {OUTPUT}  ({len(html)//1024} KB)")
