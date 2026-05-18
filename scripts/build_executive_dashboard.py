#!/usr/bin/env python3
"""
Executive Dashboard — per-pipeline grid view.

Layout:
- Top filters: Lead Source (multi-select) + Week range (start/end) — applies to ALL panels
- Grid below: one panel per WTG pipeline, each showing Created & Won by week
  (current year vs prior year)

No pipeline dropdown — every pipeline is always visible. Switch lead source
or week range to re-slice all panels at once.

Env: GOOGLE_SHEET_ID, GOOGLE_SA_JSON
Output: out/executive_dashboard.html
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials


OUT_DIR = Path(__file__).resolve().parent.parent / "out"
OUT_DIR.mkdir(exist_ok=True)
OUTPUT = OUT_DIR / "executive_dashboard.html"

GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SA_JSON = os.environ["GOOGLE_SA_JSON"]


def load_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    client = gspread.authorize(creds)
    sh = client.open_by_key(GOOGLE_SHEET_ID)
    deals = pd.DataFrame(sh.worksheet("deals_raw").get_all_records())
    return deals


print("Loading sheet…")
deals = load_sheet()
print(f"  → {len(deals)} deals")

deals["create_dt"] = pd.to_datetime(deals["create_date"], errors="coerce", utc=True).dt.tz_localize(None)
deals["won_dt"]    = pd.to_datetime(deals["won_time"],    errors="coerce", utc=True).dt.tz_localize(None)
deals = deals[deals["create_dt"].notna()].copy()

deals["lead_source"] = deals["primary_lead_source"].fillna("").astype(str).str.strip()
deals.loc[deals["lead_source"] == "", "lead_source"] = "(none)"
deals["pipeline_label"] = deals["pipeline_name"].fillna("").astype(str).str.strip()

# Lock to the 7 main WTG pipelines (both clean and Pipedrive variants).
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
deals["city"] = deals["pipeline_label"].str.lower().map(_CITY_FOR_PIPELINE)
_before = len(deals)
deals = deals[deals["city"].notna()].copy()
print(f"  Filtered to 7 main WTG cities: {len(deals):,} of {_before:,} deals")

# We will also render an "Overall" panel that aggregates ALL cities. To do this
# we duplicate every deal with city='Overall' so it appears in both its own city
# panel AND the Overall panel. (Simplest aggregation path.)
overall_rows = deals.copy()
overall_rows["city"] = "Overall"
deals = pd.concat([overall_rows, deals], ignore_index=True)

CITY_ORDER = ["Overall", "Dallas", "Houston", "San Antonio", "Austin", "Phoenix", "Utah", "Tucson"]


# ISO week labels per deal
def iso_label(ts):
    if pd.isna(ts): return None
    iy, iw, _ = ts.isocalendar()
    return f"{iy}-W{iw:02d}"

deals["cw"] = deals["create_dt"].apply(iso_label)
deals["ww"] = deals["won_dt"].apply(iso_label)

# Limit to 2024+ for size
deals = deals[deals["create_dt"] >= "2024-01-01"].copy()


# ─── Compact encoding ───
def codes(series):
    counts = series.value_counts()
    uniq = counts.index.tolist()
    labels_display = [f"{v}  ({counts[v]:,})" for v in uniq]
    code_map = {v: i for i, v in enumerate(uniq)}
    return labels_display, [code_map[v] for v in series]

ls_labels, ls_codes = codes(deals["lead_source"])
city_to_idx = {c: i for i, c in enumerate(CITY_ORDER)}
city_codes = [city_to_idx[c] for c in deals["city"]]

# Deal records: [cw, ww or null, ls_code, city_code]
records = [
    [cw, (ww if isinstance(ww, str) else None), ls, ci]
    for cw, ww, ls, ci in zip(deals["cw"], deals["ww"], ls_codes, city_codes)
]
print(f"Deal records: {len(records):,}")
print(f"Lead sources: {len(ls_labels)}, Cities: {len(CITY_ORDER)}")


# ─── Render ───
UPDATE_DATE = datetime.now(timezone.utc).strftime("%B %-d, %Y")
DATA = {
    "updateDate": UPDATE_DATE,
    "leadSources": ls_labels,
    "cities": CITY_ORDER,
    "deals": records,
}


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Executive Dashboard — Per Pipeline</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #f6f7f9; color: #1a2e4a; padding-bottom: 40px; }
.header { background: #fff; border-bottom: 1px solid #e2e8f0; padding: 16px 28px; display: flex; justify-content: space-between; align-items: center; }
.header h1 { font-size: 20px; color: #1e3a5f; }
.header .meta { font-size: 12px; color: #6b7280; }
.back { color: #1e3a5f; text-decoration: none; font-size: 13px; margin-right: 16px; }

.filters { background: #fff; border-bottom: 1px solid #e2e8f0; padding: 14px 28px; display: flex; gap: 20px; align-items: center; flex-wrap: wrap; position: sticky; top: 0; z-index: 10; }
.fg { display: flex; align-items: center; gap: 8px; position: relative; }
.fg label { font-size: 11px; font-weight: 700; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; }
.dd-btn { font-size: 13px; padding: 6px 14px; border: 1px solid #d1d5db; border-radius: 6px; background: #fff; cursor: pointer; min-width: 220px; text-align: left; }
.dd-menu { position: absolute; top: 100%; left: 0; margin-top: 4px; background: #fff; border: 1px solid #d1d5db; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); max-height: 360px; overflow-y: auto; z-index: 50; min-width: 280px; display: none; }
.dd-menu.open { display: block; }
.dd-menu label { display: block; font-size: 13px; padding: 5px 10px; cursor: pointer; }
.dd-menu label:hover { background: #f3f4f6; }
.dd-sticky { position: sticky; top: 0; background: #fff; z-index: 2; padding: 8px 8px 4px; border-bottom: 1px solid #eee; }
.dd-actions { display: flex; gap: 6px; margin-bottom: 6px; }
.dd-actions button { font-size: 11px; padding: 4px 10px; border: 1px solid #ccc; border-radius: 4px; background: #fff; cursor: pointer; }
.dd-actions button.primary { background: #1e3a5f; color: #fff; border-color: #1e3a5f; }
.dd-search { width: 100%; padding: 5px 8px; font-size: 12px; border: 1px solid #d1d5db; border-radius: 4px; }
.wk-input { width: 60px; padding: 5px 8px; font-size: 13px; border: 1px solid #d1d5db; border-radius: 6px; }
.apply-btn { font-size: 12px; padding: 6px 14px; border: 1px solid #1e3a5f; background: #1e3a5f; color: #fff; border-radius: 6px; cursor: pointer; }
.preset-btn { font-size: 11px; padding: 5px 10px; border: 1px solid #d1d5db; background: #fff; border-radius: 6px; cursor: pointer; color: #4b5563; }
.preset-btn:hover { background: #f3f4f6; }
.reset-btn { margin-left: auto; background: #fff; border: 1px solid #d1d5db; border-radius: 6px; padding: 6px 14px; font-size: 12px; color: #555; cursor: pointer; }

.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; padding: 24px; max-width: 1800px; margin: 0 auto; }
@media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } }

.panel { background: #fff; border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.05); }
.panel h3 { font-size: 16px; color: #1e3a5f; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
.panel h3 .city-tag { background: #e0f2fe; color: #075985; font-size: 11px; padding: 2px 8px; border-radius: 12px; font-weight: 600; }
.panel.overall { background: linear-gradient(180deg, #fffbeb 0%, #fff 60%); border: 1px solid #fde68a; }
.panel.overall h3 .city-tag { background: #fef3c7; color: #92400e; }
.totals { display: flex; gap: 16px; font-size: 12px; color: #6b7280; margin-bottom: 8px; }
.totals .num { font-weight: 700; color: #1e3a5f; font-size: 16px; }
.totals .vs { color: #94a3b8; }
.totals .delta.up { color: #16a34a; font-weight: 600; }
.totals .delta.down { color: #dc2626; font-weight: 600; }
.chart-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.chart-block .chart-label { font-size: 11px; font-weight: 700; color: #6b7280; text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 4px; }
.chart-block .chart-wrap { height: 140px; }

.empty { text-align: center; color: #94a3b8; font-style: italic; padding: 40px; }
footer { text-align: center; font-size: 11px; color: #9ca3af; padding: 20px; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>📊 Executive Dashboard</h1>
    <div class="meta">Per-pipeline view · Deals Created &amp; Closed by week · Filter applies to all panels</div>
  </div>
  <div>
    <a class="back" href="index.html">← All reports</a>
    <span class="meta">Last build <span id="update-date">—</span></span>
  </div>
</div>

<div class="filters">
  <div class="fg">
    <label>Lead Source</label>
    <button class="dd-btn" id="ls-btn">All Lead Sources ▾</button>
    <div class="dd-menu" id="ls-menu"></div>
  </div>
  <div class="fg">
    <label>Week Range</label>
    <input class="wk-input" type="number" id="wk-start" min="1" max="53" value="1" placeholder="Start">
    <span style="color:#9ca3af">→</span>
    <input class="wk-input" type="number" id="wk-end" min="1" max="53" value="20" placeholder="End">
    <button class="apply-btn" onclick="applyAll()">Apply</button>
    <button class="preset-btn" onclick="setRange(1,17)">Wk 1–17</button>
    <button class="preset-btn" onclick="setRange(1,13)">Q1</button>
    <button class="preset-btn" onclick="setRange(14,26)">Q2</button>
    <button class="preset-btn" onclick="setRange(27,39)">Q3</button>
    <button class="preset-btn" onclick="setRange(40,53)">Q4</button>
  </div>
  <button class="reset-btn" onclick="resetFilters()">↺ Reset</button>
</div>

<div class="grid" id="grid"></div>
<footer>Auto-updated daily · 7 main WTG pipelines · Click a panel chart legend to toggle this/prior year</footer>

<script>
const DATA = __DATA_JSON__;
document.getElementById('update-date').textContent = DATA.updateDate;

const filters = { ls: new Set() };  // empty = all

// ── Build LS dropdown ──
function buildLsDropdown() {
  const menu = document.getElementById('ls-menu');
  menu.innerHTML = '';
  const sticky = document.createElement('div'); sticky.className = 'dd-sticky';
  const actions = document.createElement('div'); actions.className = 'dd-actions';
  actions.innerHTML = `<button onclick="lsSelectAll()">All</button>
    <button onclick="lsSelectNone()">None</button>
    <button class="primary" onclick="lsApply()">Apply</button>`;
  sticky.appendChild(actions);
  const search = document.createElement('input'); search.type = 'search'; search.className = 'dd-search'; search.placeholder = 'Search…';
  search.addEventListener('click', e => e.stopPropagation());
  search.addEventListener('input', e => {
    const q = e.target.value.toLowerCase();
    menu.querySelectorAll('label').forEach(lab => {
      lab.style.display = (q === '' || lab.textContent.toLowerCase().includes(q)) ? '' : 'none';
    });
  });
  sticky.appendChild(search);
  menu.appendChild(sticky);
  DATA.leadSources.forEach((lbl, idx) => {
    const lab = document.createElement('label');
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.value = idx; cb.checked = true; cb.dataset.code = idx;
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(' ' + lbl));
    menu.appendChild(lab);
  });
  document.getElementById('ls-btn').onclick = e => { e.stopPropagation(); menu.classList.toggle('open'); };
}
function lsSelectAll(){
  document.querySelectorAll('#ls-menu label').forEach(lab => { if (lab.style.display !== 'none') lab.querySelector('input').checked = true; });
}
function lsSelectNone(){
  document.querySelectorAll('#ls-menu label').forEach(lab => { if (lab.style.display !== 'none') lab.querySelector('input').checked = false; });
}
function lsApply(){
  const all = [...document.querySelectorAll('#ls-menu input[type=checkbox]')];
  const checked = all.filter(c => c.checked).map(c => parseInt(c.dataset.code));
  if (checked.length === 0 || checked.length === all.length) {
    filters.ls = new Set();
    document.getElementById('ls-btn').textContent = 'All Lead Sources ▾';
  } else {
    filters.ls = new Set(checked);
    const display = checked.length === 1
      ? DATA.leadSources[checked[0]].split('  (')[0]
      : `${checked.length} selected`;
    document.getElementById('ls-btn').textContent = display + ' ▾';
  }
  document.getElementById('ls-menu').classList.remove('open');
  refresh();
}
document.addEventListener('click', e => {
  if (!document.getElementById('ls-menu').contains(e.target) && !document.getElementById('ls-btn').contains(e.target)) {
    document.getElementById('ls-menu').classList.remove('open');
  }
});

function resetFilters(){
  filters.ls.clear();
  document.getElementById('ls-btn').textContent = 'All Lead Sources ▾';
  document.querySelectorAll('#ls-menu input[type=checkbox]').forEach(c => c.checked = true);
  setRange(1, 20);
}

// ── Date / week helpers ──
function todayISO(){
  const d = new Date();
  const target = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const dayNum = target.getUTCDay() || 7;
  target.setUTCDate(target.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(target.getUTCFullYear(), 0, 1));
  return { year: target.getUTCFullYear(), week: Math.ceil((((target - yearStart) / 86400000) + 1) / 7) };
}

const currentYear = todayISO().year;

// ── Per-pipeline aggregation ──
function aggregate(startWk, endWk) {
  // Returns { [city]: { created: [byWeek], won: [byWeek], createdPrev, wonPrev } }
  const out = {};
  for (const c of DATA.cities) {
    const n = endWk - startWk + 1;
    out[c] = { created: Array(n).fill(0), won: Array(n).fill(0), createdPrev: Array(n).fill(0), wonPrev: Array(n).fill(0) };
  }
  for (const d of DATA.deals) {
    // d = [cw, ww, lsCode, cityCode]
    if (filters.ls.size > 0 && !filters.ls.has(d[2])) continue;
    const city = DATA.cities[d[3]];
    const target = out[city]; if (!target) continue;
    if (d[0]) {
      const [y,w] = d[0].split('-W').map(Number);
      if (w >= startWk && w <= endWk) {
        if (y === currentYear)     target.created[w - startWk]++;
        else if (y === currentYear-1) target.createdPrev[w - startWk]++;
      }
    }
    if (d[1]) {
      const [y,w] = d[1].split('-W').map(Number);
      if (w >= startWk && w <= endWk) {
        if (y === currentYear)     target.won[w - startWk]++;
        else if (y === currentYear-1) target.wonPrev[w - startWk]++;
      }
    }
  }
  return out;
}

const charts = {};
function renderPanel(city, data, startWk, endWk) {
  const labels = [];
  for (let w = startWk; w <= endWk; w++) labels.push(`W${w}`);
  const curC = data.created, prevC = data.createdPrev, curW = data.won, prevW = data.wonPrev;
  const sumCurC = curC.reduce((a,b)=>a+b,0), sumPrevC = prevC.reduce((a,b)=>a+b,0);
  const sumCurW = curW.reduce((a,b)=>a+b,0), sumPrevW = prevW.reduce((a,b)=>a+b,0);
  const pctC = sumPrevC > 0 ? Math.round((sumCurC/sumPrevC - 1)*100) : null;
  const pctW = sumPrevW > 0 ? Math.round((sumCurW/sumPrevW - 1)*100) : null;
  const deltaC = pctC === null ? '' : `<span class="delta ${pctC>=0?'up':'down'}">${pctC>=0?'▲':'▼'} ${Math.abs(pctC)}%</span>`;
  const deltaW = pctW === null ? '' : `<span class="delta ${pctW>=0?'up':'down'}">${pctW>=0?'▲':'▼'} ${Math.abs(pctW)}%</span>`;

  const id = city.replace(/\\s+/g,'-').toLowerCase();
  const panel = document.getElementById(`p-${id}`) || (() => {
    const p = document.createElement('div');
    p.className = 'panel' + (city === 'Overall' ? ' overall' : '');
    p.id = `p-${id}`;
    p.innerHTML = `
      <h3>${city} <span class="city-tag">${city.toUpperCase()}</span></h3>
      <div class="totals">
        <div><span class="num" id="${id}-c-cur">0</span> created <span class="vs">vs <span id="${id}-c-prev">0</span> LY</span> <span id="${id}-c-delta"></span></div>
        <div><span class="num" id="${id}-w-cur">0</span> won <span class="vs">vs <span id="${id}-w-prev">0</span> LY</span> <span id="${id}-w-delta"></span></div>
      </div>
      <div class="chart-row">
        <div class="chart-block"><div class="chart-label">Created</div><div class="chart-wrap"><canvas id="${id}-c-chart"></canvas></div></div>
        <div class="chart-block"><div class="chart-label">Won</div><div class="chart-wrap"><canvas id="${id}-w-chart"></canvas></div></div>
      </div>`;
    document.getElementById('grid').appendChild(p);
    return p;
  })();

  document.getElementById(`${id}-c-cur`).textContent = sumCurC.toLocaleString();
  document.getElementById(`${id}-c-prev`).textContent = sumPrevC.toLocaleString();
  document.getElementById(`${id}-c-delta`).innerHTML = deltaC;
  document.getElementById(`${id}-w-cur`).textContent = sumCurW.toLocaleString();
  document.getElementById(`${id}-w-prev`).textContent = sumPrevW.toLocaleString();
  document.getElementById(`${id}-w-delta`).innerHTML = deltaW;

  const makeChart = (canvasId, cur, prev, color) => {
    if (charts[canvasId]) {
      charts[canvasId].data.labels = labels;
      charts[canvasId].data.datasets[0].data = cur;
      charts[canvasId].data.datasets[1].data = prev;
      charts[canvasId].update();
      return;
    }
    charts[canvasId] = new Chart(document.getElementById(canvasId), {
      type: 'bar',
      data: { labels, datasets: [
        { label: 'This Year', data: cur, backgroundColor: color },
        { label: 'Prior Year', data: prev, backgroundColor: color+'40' },
      ]},
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom', labels: { font: { size: 9 }, boxWidth: 10 } } },
        scales: { x: { grid: { display: false }, ticks: { font: { size: 9 } } },
                  y: { grid: { color: '#f0f0f0' }, ticks: { font: { size: 9 } } } } }
    });
  };
  makeChart(`${id}-c-chart`, curC, prevC, '#1e40af');
  makeChart(`${id}-w-chart`, curW, prevW, '#16a34a');
}

function refresh() {
  const s = parseInt(document.getElementById('wk-start').value) || 1;
  const e = parseInt(document.getElementById('wk-end').value) || 20;
  if (e < s) return;
  const agg = aggregate(s, e);
  DATA.cities.forEach(c => renderPanel(c, agg[c], s, e));
}
function applyAll() { refresh(); }
function setRange(s, e) {
  document.getElementById('wk-start').value = s;
  document.getElementById('wk-end').value = e;
  refresh();
}

buildLsDropdown();
refresh();
</script>
</body>
</html>
"""

html = HTML.replace("__DATA_JSON__", json.dumps(DATA, default=str))
OUTPUT.write_text(html, encoding="utf-8")
print(f"  ✓ Written: {OUTPUT}  ({len(html)//1024} KB)")
