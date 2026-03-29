#!/usr/bin/env python3
"""
Preview Python-computed insights from real Firestore data.
Run from project root: python scripts/insights_preview.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud.firestore_v1.base_query import FieldFilter
from bluesky.shared.firestore_client import db

def main():
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)
    start_iso = start.isoformat()

    print(f"Querying engagement_events since {start_iso}...")
    events = [
        d.to_dict()
        for d in db.collection("engagement_events")
        .where(filter=FieldFilter("created_at", ">=", start_iso))
        .stream()
    ]
    print(f"  {len(events)} events found.\n")

    settings_doc = db.collection("_system").document("settings").get()
    settings = settings_doc.to_dict() or {} if settings_doc.exists else {}
    discount_cap = int(settings.get("caps", {}).get("max_discounts_per_day", 5))

    # Compute stats
    fan_replies_out = sum(1 for e in events if e.get("type") == "reply" and e.get("direction") == "outbound" and e.get("user_type") == "fan")
    dm_pulls        = sum(1 for e in events if e.get("reply_type") == "fan_dm_pull")
    buying_signals  = sum(1 for e in events if e.get("fan_intent") in ("buying_signal", "curious"))
    handoffs        = sum(1 for e in events if e.get("interaction_subtype") == "handoff")
    sub_guard       = sum(1 for e in events if e.get("reply_type") == "subscriber_warmth")
    discounts_sent  = sum(1 for e in events if e.get("interaction_subtype") == "discount_sent")
    dms_sent        = sum(1 for e in events if e.get("type") == "dm" and e.get("direction") == "outbound")
    follows_in      = sum(1 for e in events if e.get("type") == "follow" and e.get("direction") == "inbound")

    print("--- Raw counts ---")
    print(f"  fan_replies_out : {fan_replies_out}")
    print(f"  dm_pulls        : {dm_pulls}")
    print(f"  buying_signals  : {buying_signals}")
    print(f"  sub_guard       : {sub_guard}")
    print(f"  discounts_sent  : {discounts_sent}  (cap: {discount_cap})")
    print(f"  handoffs        : {handoffs}")
    print(f"  dms_sent        : {dms_sent}")
    print(f"  follows_in      : {follows_in}")
    print()

    # Build insights
    insights = []

    if fan_replies_out > 0:
        rate = round(dm_pulls / fan_replies_out * 100)
        insights.append(f"DM pull rate: {dm_pulls} of {fan_replies_out} fan replies converted ({rate}%).")
    else:
        insights.append("No fan replies in the last 24h.")

    if buying_signals > 0:
        insights.append(f"{buying_signals} buying or curious signal{'s' if buying_signals != 1 else ''} detected.")

    if sub_guard > 0:
        insights.append(f"Funnel skipped on {sub_guard} possible subscriber{'s' if sub_guard != 1 else ''}.")

    if discount_cap > 0 and discounts_sent > 0:
        pct = round(discounts_sent / discount_cap * 100)
        insights.append(f"Discount cap at {pct}% — {discounts_sent} of {discount_cap} sent today.")

    if handoffs > 0:
        insights.append(f"{handoffs} conversation{'s' if handoffs != 1 else ''} handed off for a personal reply.")

    if dms_sent > 0:
        insights.append(f"{dms_sent} outbound DM{'s' if dms_sent != 1 else ''} sent.")

    if follows_in > 0:
        insights.append(f"{follows_in} new follower{'s' if follows_in != 1 else ''} in the last 24h.")

    if not insights:
        insights.append("No activity in the last 24h.")

    print("--- Insights ---")
    for ins in insights:
        print(f"  {ins}")

if __name__ == "__main__":
    main()
