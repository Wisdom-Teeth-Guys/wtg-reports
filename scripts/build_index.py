#!/usr/bin/env python3
"""Generate the landing page index.html and copy archived reports into out/."""

import shutil
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR     = REPO_ROOT / "out"
ARCHIVE_SRC = REPO_ROOT / "archive"
ARCHIVE_OUT = OUT_DIR / "archive"
MK_SRC      = REPO_ROOT / "marketer_reports"
MK_OUT      = OUT_DIR / "marketer_reports"

OUT_DIR.mkdir(exist_ok=True)
ARCHIVE_OUT.mkdir(exist_ok=True)
MK_OUT.mkdir(exist_ok=True)

# Copy archived (point-in-time) reports into the deploy output
archived = []
if ARCHIVE_SRC.exists():
    for f in sorted(ARCHIVE_SRC.glob("*.html")):
        shutil.copy(f, ARCHIVE_OUT / f.name)
        archived.append(f.name)
        print(f"  ✓ archived: {f.name}")

# Copy marketer reports + read metadata from marketers.yaml
marketers = []
if MK_SRC.exists():
    yaml_path = MK_SRC / "marketers.yaml"
    metadata = {}
    if yaml_path.exists():
        # Tiny inline YAML parser — only supports the simple structure we use
        try:
            import yaml
            doc = yaml.safe_load(yaml_path.read_text())
            for slug, info in (doc.get("marketers") or {}).items():
                metadata[slug] = info
        except Exception as e:
            print(f"  ! couldn't parse marketers.yaml: {e}")
    for f in sorted(MK_SRC.glob("*.html")):
        slug = f.stem
        shutil.copy(f, MK_OUT / f.name)
        info = metadata.get(slug, {})
        marketers.append({
            "filename": f.name,
            "name":     info.get("name", slug.replace("_", " ").title()),
            "territory": info.get("territory", ""),
        })
        print(f"  ✓ marketer: {f.name}")

# Copy call-center ops dashboard(s) into the deploy output
OPS_SRC = REPO_ROOT / "ops"
OPS_OUT = OUT_DIR / "ops"
ops_files = []
if OPS_SRC.exists():
    OPS_OUT.mkdir(exist_ok=True)
    for f in sorted(OPS_SRC.glob("*.html")):
        shutil.copy(f, OPS_OUT / f.name)
        ops_files.append(f.name)
        print(f"  ✓ ops: {f.name}")

UPDATED = datetime.now(timezone.utc).strftime('%B %-d, %Y at %-I:%M %p UTC')

ARCHIVE_LABELS = {
    "slowdown_analysis_2026-05-04.html":               ("🔍 Slowdown Analysis", "Week of May 4, 2026 — deep dive into pipeline bottlenecks."),
    "dental_ortho_referrals_apr2025_mar2026.html":     ("🦷 Dental/Ortho Referrals (All)", "April 2025 – March 2026. All deals with filters and charts."),
    "dental_ortho_referrals_won_apr2025_mar2026.html": ("✅ Dental/Ortho Referrals (Won)", "April 2025 – March 2026. Won deals only."),
    "ppo_mapping_review.html":                         ("🗂️ PPO Mapping Review", "Pipedrive → HubSpot insurance plan reconciliation tool."),
}

archive_cards = ""
for fname in archived:
    label, desc = ARCHIVE_LABELS.get(fname, (fname, ""))
    archive_cards += f"""
  <a class="card archive" target="_blank" rel="noopener" href="archive/{fname}">
    <h3>{label}</h3>
    <p>{desc}</p>
  </a>"""

marketer_cards = ""
for m in marketers:
    territory_tag = f" · {m['territory']}" if m['territory'] else ""
    marketer_cards += f"""
  <a class="card marketer" target="_blank" rel="noopener" href="marketer_reports/{m['filename']}">
    <h3>👤 {m['name']}</h3>
    <p>Individual marketer report{territory_tag}.</p>
  </a>"""

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>WTG Reports</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #f0f4f8; color: #1a202c; padding: 40px 20px; }}
.container {{ max-width: 1000px; margin: 0 auto; }}
header {{ margin-bottom: 32px; }}
header h1 {{ color: #0a4d8c; font-size: 28px; }}
header .meta {{ color: #718096; font-size: 14px; margin-top: 6px; }}
.section-title {{ font-size: 13px; font-weight: 700; color: #475569; text-transform: uppercase;
                   letter-spacing: 0.08em; margin: 32px 0 12px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }}
.card {{ background: white; padding: 22px; border-radius: 12px; border: 1px solid #e2e8f0;
         text-decoration: none; color: inherit; transition: box-shadow .15s, transform .15s; display: block; }}
.card:hover {{ box-shadow: 0 4px 18px rgba(10, 77, 140, 0.10); transform: translateY(-2px); }}
.card.live {{ border-left: 4px solid #0a4d8c; }}
.card.archive {{ border-left: 4px solid #94a3b8; }}
.card.marketer {{ border-left: 4px solid #7c3aed; }}
.card h3 {{ color: #0a4d8c; font-size: 16px; margin-bottom: 6px; }}
.card p {{ color: #475569; font-size: 13px; line-height: 1.55; }}
.live-badge {{ display: inline-block; font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 4px;
                background: #ecfdf5; color: #065f46; margin-left: 6px; vertical-align: middle; }}
footer {{ margin-top: 40px; text-align: center; color: #a0aec0; font-size: 12px; }}
</style>
</head><body>
<div class="container">
<header>
  <h1>WTG Internal Reports</h1>
  <div class="meta">Auto-updated daily from HubSpot · Last build {UPDATED}</div>
</header>

<div class="section-title">Live Dashboards <span class="live-badge">AUTO-UPDATING</span></div>
<div class="grid">
  <a class="card live" target="_blank" rel="noopener" href="pipeline_dashboard.html">
    <h3>📊 Referral Deals Created Pipeline</h3>
    <p>Account performance, at-risk alerts, tier breakdown, and YoY trend by territory and rep.</p>
  </a>
  <a class="card live" target="_blank" rel="noopener" href="deal_won_time_dashboard.html">
    <h3>⏱️ Referral Deals Closed</h3>
    <p>Won-deals only. Same account view filtered to closed-won deals.</p>
  </a>
  <a class="card live" target="_blank" rel="noopener" href="executive_dashboard.html">
    <h3>📊 Executive Dashboard</h3>
    <p>Deals Created (forward indicator) + Deals Closed by week. Filter by Lead Source, Pipeline, and Territory.</p>
  </a>
  <a class="card live" target="_blank" rel="noopener" href="google_ads_dashboard.html">
    <h3>💰 Google Ads Dashboard</h3>
    <p>Google Ads spend by city + HubSpot deals attributed to Google Adwords PPC. Effective CPA by location.</p>
  </a>
</div>

<div class="section-title">Call-Center Operations</div>
<div class="grid">
  <a class="card live" target="_blank" rel="noopener" href="ops/ops_dashboard.html">
    <h3>📞 Call-Center Ops &amp; Staffing</h3>
    <p>Daily answer rate, call volume, abandons, wait time, and agents-active-by-hour — by market and agent, with YoY/WoW. Built for staffing decisions.</p>
  </a>
</div>

<div class="section-title">Individual Marketer Reports</div>
<div class="grid">{marketer_cards}
</div>

<div class="section-title">Archived Reports (Point-in-Time)</div>
<div class="grid">{archive_cards}
</div>

<footer>Access managed via Cloudflare Zero Trust · Live data from HubSpot</footer>
</div></body></html>
"""

(OUT_DIR / "index.html").write_text(HTML, encoding="utf-8")
print(f"  ✓ index.html")
print(f"  ✓ {len(archived)} archived report(s) staged in out/archive/")
print(f"  ✓ {len(marketers)} marketer report(s) staged in out/marketer_reports/")
