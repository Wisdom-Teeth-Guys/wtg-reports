"""
Weekly visit intelligence refresher.

Pulls the last N days of SPOTIO visits, classifies the most recent outcome per
office, recomputes `consecutive_closed_count`, and pushes to HubSpot.

This is the script you run on a weekly cron (Sunday 7:30pm) BEFORE the route
builder runs (Sunday 8:00pm). It ensures the route scoring reflects every
visit completed that week.

Internally a thin wrapper around `spotio_backfill.main()` with a tighter
default time window (last 14 days vs. all-history).

Usage:
    python3 -m route_builder.visit_intelligence_updater                   # dry-run, last 14 days
    python3 -m route_builder.visit_intelligence_updater --push            # actually update HubSpot
    python3 -m route_builder.visit_intelligence_updater --days 30         # custom window
    python3 -m route_builder.visit_intelligence_updater --since 2026-05-01
"""
import argparse
import sys
from datetime import date, timedelta

from .spotio_backfill import main as backfill_main


def main():
    p = argparse.ArgumentParser(description="Weekly SPOTIO → HubSpot visit intelligence refresher")
    p.add_argument("--days", type=int, default=14,
                   help="Look back this many days (default: 14)")
    p.add_argument("--since", help="Override start date (YYYY-MM-DD) — takes precedence over --days")
    p.add_argument("--limit", type=int, help="Cap matched leads (for testing)")
    p.add_argument("--push", action="store_true",
                   help="Actually write to HubSpot (default: dry-run)")
    p.add_argument("--output-dir", help="Override output directory")
    args = p.parse_args()

    since = args.since or (date.today() - timedelta(days=args.days)).isoformat()
    print(f"Visit intelligence refresh — looking at SPOTIO visits since {since}\n")
    return backfill_main(since=since, limit=args.limit, push=args.push,
                          output_dir=args.output_dir)


if __name__ == "__main__":
    sys.exit(main())
