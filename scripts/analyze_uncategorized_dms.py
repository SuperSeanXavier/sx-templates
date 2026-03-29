#!/usr/bin/env python3
"""
Analyze uncategorized outbound DM engagement_events.

For each DM event missing interaction_subtype (or set to "other"),
looks up the conversations doc for that handle and infers what the
subtype would have been from trigger_context.

Run from project root:
    python scripts/analyze_uncategorized_dms.py
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bluesky.shared.firestore_client import db


TRIGGER_TO_SUBTYPE = {
    "like":          "like_trigger",
    "repost":        "repost_trigger",
    "follow":        "follow_trigger",
    "reply_dm_pull": "reply_dm_pull",
}


def main():
    print("Fetching outbound DM engagement_events...")
    all_dm_events = [
        d.to_dict()
        for d in db.collection("engagement_events")
        .where("type", "==", "dm")
        .where("direction", "==", "outbound")
        .stream()
    ]
    print(f"  {len(all_dm_events)} total outbound DM events\n")

    uncategorized = [
        e for e in all_dm_events
        if not e.get("interaction_subtype") or e.get("interaction_subtype") == "other"
    ]
    categorized = [e for e in all_dm_events if e not in uncategorized]

    print(f"Already categorized : {len(categorized)}")
    print(f"Uncategorized       : {len(uncategorized)}\n")

    if not uncategorized:
        print("Nothing to analyze.")
        return

    # Count existing categories for reference
    existing = Counter(e.get("interaction_subtype", "other") for e in categorized)
    if existing:
        print("--- Existing categorized breakdown ---")
        for subtype, count in existing.most_common():
            print(f"  {subtype:<30} {count}")
        print()

    # Infer categories from conversations.trigger_context
    print("Looking up conversations docs for uncategorized handles...")
    inferred = Counter()
    no_convo = []
    unknown_trigger = Counter()

    handles = list({e.get("handle") for e in uncategorized if e.get("handle")})
    convo_map = {}
    for handle in handles:
        doc = db.collection("conversations").document(handle).get()
        if doc.exists:
            convo_map[handle] = doc.to_dict() or {}

    for event in uncategorized:
        handle = event.get("handle")
        convo = convo_map.get(handle)
        if not convo:
            no_convo.append(handle)
            inferred["no_conversation_record"] += 1
            continue
        trigger = convo.get("trigger_context", "")
        subtype = TRIGGER_TO_SUBTYPE.get(trigger)
        if subtype:
            inferred[subtype] += 1
        else:
            unknown_trigger[trigger or "(empty)"] += 1
            inferred["unknown_trigger"] += 1

    print(f"\n--- Inferred breakdown for {len(uncategorized)} uncategorized DMs ---")
    for subtype, count in inferred.most_common():
        pct = round(count / len(uncategorized) * 100)
        print(f"  {subtype:<30} {count:>4}  ({pct}%)")

    if unknown_trigger:
        print(f"\n--- Unknown trigger_context values ---")
        for val, count in unknown_trigger.most_common():
            print(f"  '{val}': {count}")

    if no_convo:
        print(f"\n--- {len(no_convo)} handles had no conversations doc ---")
        for h in no_convo[:10]:
            print(f"  @{h}")
        if len(no_convo) > 10:
            print(f"  ... and {len(no_convo) - 10} more")

    print(f"\n--- Projected full breakdown (categorized + inferred) ---")
    projected = Counter(e.get("interaction_subtype") for e in categorized)
    projected += {k: v for k, v in inferred.items() if k not in ("no_conversation_record", "unknown_trigger")}
    total = sum(projected.values())
    for subtype, count in projected.most_common():
        pct = round(count / total * 100) if total else 0
        print(f"  {subtype:<30} {count:>4}  ({pct}%)")


if __name__ == "__main__":
    main()
