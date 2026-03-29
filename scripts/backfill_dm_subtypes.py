#!/usr/bin/env python3
"""
Backfill interaction_subtype on uncategorized outbound DM engagement_events.

For each DM event missing interaction_subtype (or set to "other"),
looks up conversations.trigger_context and writes the correct subtype back.

Run from project root:
    python scripts/backfill_dm_subtypes.py           # dry-run (default)
    python scripts/backfill_dm_subtypes.py --write   # live write
"""
import os
import sys
import argparse
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bluesky.shared.firestore_client import db
from google.cloud.firestore_v1.base_query import FieldFilter


TRIGGER_TO_SUBTYPE = {
    "like":          "like_trigger",
    "repost":        "repost_trigger",
    "follow":        "follow_trigger",
    "reply_dm_pull": "reply_dm_pull",
}


def main(write: bool):
    mode = "LIVE WRITE" if write else "DRY RUN"
    print(f"[{mode}] Fetching uncategorized outbound DM events...\n")

    all_dm_docs = [
        (d.id, d.to_dict())
        for d in db.collection("engagement_events")
        .where(filter=FieldFilter("type", "==", "dm"))
        .where(filter=FieldFilter("direction", "==", "outbound"))
        .stream()
    ]

    uncategorized = [
        (doc_id, data) for doc_id, data in all_dm_docs
        if not data.get("interaction_subtype") or data.get("interaction_subtype") == "other"
    ]
    print(f"  {len(all_dm_docs)} total outbound DM events")
    print(f"  {len(uncategorized)} uncategorized\n")

    if not uncategorized:
        print("Nothing to backfill.")
        return

    # Load conversation docs for all relevant handles
    handles = list({data.get("handle") for _, data in uncategorized if data.get("handle")})
    print(f"  Loading {len(handles)} conversation docs...")
    convo_map = {}
    for handle in handles:
        doc = db.collection("conversations").document(handle).get()
        if doc.exists:
            convo_map[handle] = doc.to_dict() or {}

    # Patch
    patched = Counter()
    skipped = Counter()

    for doc_id, data in uncategorized:
        handle = data.get("handle")
        convo = convo_map.get(handle)
        if not convo:
            skipped["no_conversation_record"] += 1
            continue
        trigger = convo.get("trigger_context", "")
        subtype = TRIGGER_TO_SUBTYPE.get(trigger)
        if not subtype:
            skipped[f"unknown_trigger:{trigger or '(empty)'}"] += 1
            continue

        if write:
            db.collection("engagement_events").document(doc_id).update({
                "interaction_subtype": subtype
            })
        else:
            print(f"  [dry-run] @{handle} → {subtype}")

        patched[subtype] += 1

    print(f"\n--- {'Patched' if write else 'Would patch'} ---")
    for subtype, count in patched.most_common():
        print(f"  {subtype:<30} {count}")

    if skipped:
        print(f"\n--- Skipped ---")
        for reason, count in skipped.most_common():
            print(f"  {reason:<40} {count}")

    total = sum(patched.values())
    print(f"\n  Total: {total} record(s) {'updated' if write else 'ready to update'}")
    if not write:
        print("\n  Re-run with --write to apply changes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Write changes to Firestore (default: dry-run)")
    args = parser.parse_args()
    main(write=args.write)
