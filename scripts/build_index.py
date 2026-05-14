#!/usr/bin/env python3
"""Generate the landing page index.html that links to both dashboards."""

from datetime import datetime, timezone
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "out"
OUT_DIR.mkdir(exist_ok=True)

UPDATED = datetime.now(timezone.utc).strftime('%B %-d, %Y at %-I:%M %p UTC')

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
.container {{ max-width: 900px; margin: 0 auto; }}
header {{ margin-bottom: 32px; }}
header h1 {{ color: #0a4d8c; font-size: 28px; }}
header .meta {{ color: #718096; font-size: 14px; margin-top: 6px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; }}
.card {{ background: white; padding: 28px; border-radius: 14px; border: 1px solid #e2e8f0;
         text-decoration: none; color: inherit; transition: box-shadow .15s, transform .15s; display: block; }}
.card:hover {{ box-shadow: 0 6px 24px rgba(10, 77, 140, 0.12); transform: translateY(-3px); }}
.card .icon {{ font-size: 32px; margin-bottom: 12px; }}
.card h3 {{ color: #0a4d8c; font-size: 18px; margin-bottom: 8px; }}
.card p {{ color: #4a5568; font-size: 14px; line-height: 1.55; }}
footer {{ margin-top: 40px; text-align: center; color: #a0aec0; font-size: 12px; }}
</style>
</head><body>
<div class="container">
<header>
  <h1>WTG Internal Reports</h1>
  <div class="meta">Live data from HubSpot · Last updated {UPDATED}</div>
</header>
<div class="grid">
  <a class="card" href="pipeline_dashboard.html">
    <div class="icon">📊</div>
    <h3>Pipeline Action Dashboard</h3>
    <p>Account performance, at-risk alerts, tier breakdown, and YoY trend by territory and rep.</p>
  </a>
  <a class="card" href="deal_won_time_dashboard.html">
    <div class="icon">⏱️</div>
    <h3>Deal Won Time Dashboard</h3>
    <p>Won-deals only. Same account view filtered to closed-won, with won-time metrics.</p>
  </a>
</div>
<footer>Auto-generated daily · Access managed via Cloudflare Zero Trust</footer>
</div></body></html>
"""

(OUT_DIR / "index.html").write_text(HTML, encoding="utf-8")
print(f"  ✓ index.html ({len(HTML)} bytes)")
