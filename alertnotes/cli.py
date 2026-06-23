#!/usr/bin/env python3
"""
AlertNotes CLI
Query your alert resolution history directly from the terminal.

Usage:
  alertnotes why "HighMemoryUsage"
  alertnotes why "CPUThrottle" --service api-gateway
  alertnotes list
  alertnotes stats
"""

import argparse
import json
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from alertnotes import database as db


def cmd_why(args):
    db.init_db()
    alert_name = args.alert_name

    # Find matching alerts
    all_alerts = db.get_alert_history(limit=200, search=alert_name)
    if args.service:
        all_alerts = [a for a in all_alerts if (a.get("service") or "").lower() == args.service.lower()]

    if not all_alerts:
        print(f"\n❓ No alerts found matching '{alert_name}'")
        if args.service:
            print(f"   (filtered by service: {args.service})")
        print("\n   Either the alert hasn't fired yet, or no resolutions have been documented.")
        return

    for alert in all_alerts[:3]:
        fingerprint = alert["fingerprint"]
        resolutions = db.get_resolutions(fingerprint, limit=args.limit)

        print(f"\n{'─' * 60}")
        print(f"🔔  {alert['alert_name']}")
        print(f"    Service: {alert.get('service') or 'unknown'} | "
              f"Environment: {alert.get('environment', 'production')} | "
              f"Fired {alert.get('fire_count', 1)} time(s)")
        print(f"{'─' * 60}")

        if not resolutions:
            print("\n  📭 No documented resolutions yet.")
            print(f"  First time this fires, visit the resolution form to document it.")
            continue

        for i, res in enumerate(resolutions, 1):
            fired_dt = datetime.fromisoformat(res["fired_at"])
            date_str = fired_dt.strftime("%b %d, %Y %H:%M UTC")
            duration_min = round((res.get("duration_secs") or 0) / 60)
            resolved_by = res.get("resolved_by") or "unknown"

            print(f"\n  #{i} — {date_str} · {duration_min}m · {resolved_by}")

            if res.get("cause"):
                print(f"\n  Cause:")
                for line in _wrap(res["cause"], 70):
                    print(f"    {line}")

            if res.get("fix"):
                print(f"\n  Fix:")
                for line in _wrap(res["fix"], 70):
                    print(f"    {line}")

            commits = json.loads(res.get("commits") or "[]")
            if commits:
                print(f"\n  Related commits:")
                for c in commits[:3]:
                    print(f"    [{c['sha']}] {c['message'][:70]} — {c['author']}")

            print()


def cmd_list(args):
    db.init_db()
    alerts = db.get_alert_history(limit=args.limit, search=args.search)

    if not alerts:
        print("\n📭 No alerts in the database yet.")
        return

    print(f"\n{'Alert Name':<35} {'Service':<20} {'Fires':>6} {'Documented':>11} {'Last Seen':<12}")
    print("─" * 92)

    for a in alerts:
        res_count = a.get("resolution_count", 0)
        documented = f"✅ {res_count}" if res_count else "❌ none"
        last_seen = (a.get("last_seen") or "")[:10]
        name = (a["alert_name"] or "")[:34]
        service = (a.get("service") or "—")[:19]
        print(f"{name:<35} {service:<20} {a.get('fire_count', 1):>6} {documented:>11} {last_seen:<12}")

    print(f"\n  {len(alerts)} alert(s) total.")


def cmd_stats(args):
    db.init_db()
    stats = db.get_stats()

    print("\n📊 AlertNotes Statistics")
    print("─" * 40)
    print(f"  Unique alert types:       {stats['total_alerts']}")
    print(f"  Documented resolutions:   {stats['total_resolutions']}")
    print(f"  Avg resolution time:      {stats['avg_resolution_mins']} min")
    print(f"  Times history was helpful:{stats['helpful_count']}")

    if stats.get("top_recurring_alerts"):
        print("\n  Top recurring alerts:")
        for a in stats["top_recurring_alerts"]:
            print(f"    • {a['alert_name']} ({a.get('service','?')}) — {a['fire_count']}x")

    print()


def _wrap(text: str, width: int) -> list:
    words = text.split()
    lines, line = [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return lines


def main():
    parser = argparse.ArgumentParser(
        prog="alertnotes",
        description="Query your alert resolution history",
    )
    sub = parser.add_subparsers(dest="command")

    # why command
    why = sub.add_parser("why", help="Show resolution history for an alert")
    why.add_argument("alert_name", help="Alert name to query (partial match)")
    why.add_argument("--service", "-s", help="Filter by service name")
    why.add_argument("--limit", "-n", type=int, default=5, help="Max resolutions to show")

    # list command
    lst = sub.add_parser("list", help="List all alerts in the database")
    lst.add_argument("--search", help="Filter by name or service")
    lst.add_argument("--limit", "-n", type=int, default=50)

    # stats command
    sub.add_parser("stats", help="Show overall statistics")

    args = parser.parse_args()

    if args.command == "why":
        cmd_why(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()