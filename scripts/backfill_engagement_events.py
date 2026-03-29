#!/usr/bin/env python3
"""
Backfill engagement_events from historical Firestore data.

Sources:
  - conversations       → outbound DM outreach events
  - conversations/messages subcollection → inbound fan replies + outbound bot replies
  - dm_queue (sent)     → queued DMs that were sent (follows)

Run once from the project root:
  python scripts/backfill_engagement_events.py

Dry-run (prints what would be written, no Firestore writes):
  python scripts/backfill_engagement_events.py --dry-run
"""
import os
import sys
import argparse
from datetime import timezone

# Allow running from project root without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv("bluesky/reply/.env")
except ImportError:
    pass

from bluesky.shared.firestore_client import db
from google.cloud import firestore


def _ts(value):
    """Normalise a timestamp to an ISO string."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        # datetime or DatetimeWithNanoseconds
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def backfill(dry_run=False):
    label = "[DRY RUN] " if dry_run else ""
    total = 0

    # ------------------------------------------------------------------
    # 1. conversations → one outbound DM event per conversation
    # ------------------------------------------------------------------
    print("Reading conversations...")
    convos = list(db.collection("conversations").stream())
    print(f"  Found {len(convos)} conversation(s)")

    for doc in convos:
        d = doc.to_dict()
        handle = d.get("fan_handle") or doc.id
        trigger = d.get("trigger_context", "like")
        created_at = _ts(d.get("created_at"))

        event = {
            "type": "dm",
            "direction": "outbound",
            "handle": handle,
            "post_uri": None,
            "reply_type": "dm_outreach",
            "interaction_subtype": f"{trigger}_trigger",
            "user_type": d.get("user_type"),
            "stage": d.get("stage"),
            "created_at": created_at,
            # enrichment fields — not available for historical data
            "fan_intent": None,
            "mirror_tier": None,
            "post_type_classification": None,
            "token_usage_input": None,
            "token_usage_output": None,
            "model": None,
            "_backfilled": True,
            "_source": "conversations",
        }

        print(f"  {label}outreach DM → @{handle} ({trigger}, {created_at})")
        if not dry_run:
            db.collection("engagement_events").add(event)
        total += 1

    # ------------------------------------------------------------------
    # 2. conversations/messages → inbound fan messages + bot replies
    # ------------------------------------------------------------------
    print("\nReading messages subcollections...")
    msg_total = 0

    for convo_doc in convos:
        handle = convo_doc.to_dict().get("fan_handle") or convo_doc.id
        msgs = list(
            convo_doc.reference.collection("messages")
            .order_by("timestamp")
            .stream()
        )

        for msg in msgs:
            m = msg.to_dict()
            role = m.get("role", "assistant")
            direction = "inbound" if role == "user" else "outbound"
            timestamp = _ts(m.get("timestamp"))

            event = {
                "type": "dm",
                "direction": direction,
                "handle": handle,
                "post_uri": None,
                "reply_type": "dm_conversation",
                "interaction_subtype": None,
                "created_at": timestamp,
                # enrichment fields — not available for historical data
                "fan_intent": None,
                "mirror_tier": None,
                "post_type_classification": None,
                "token_usage_input": None,
                "token_usage_output": None,
                "model": None,
                "_backfilled": True,
                "_source": "messages",
            }

            print(f"  {label}message ({direction}) @{handle} {timestamp}")
            if not dry_run:
                db.collection("engagement_events").add(event)
            msg_total += 1

    print(f"  {msg_total} message event(s)")
    total += msg_total

    # ------------------------------------------------------------------
    # 3. dm_queue (status=sent) → queued DMs that were delivered
    # ------------------------------------------------------------------
    print("\nReading dm_queue (sent)...")
    from google.cloud.firestore_v1.base_query import FieldFilter as _filter

    sent_docs = list(
        db.collection("dm_queue")
        .where(filter=_filter("status", "==", "sent"))
        .stream()
    )
    print(f"  Found {len(sent_docs)} sent queue item(s)")

    for doc in sent_docs:
        d = doc.to_dict()
        handle = d.get("fan_handle", "unknown")
        trigger = d.get("trigger_type", "follow")
        sent_at = _ts(d.get("sent_at")) or _ts(d.get("created_at"))

        event = {
            "type": "dm",
            "direction": "outbound",
            "handle": handle,
            "post_uri": None,
            "reply_type": "dm_outreach",
            "interaction_subtype": f"{trigger}_trigger",
            "user_type": d.get("user_type"),
            "stage": "warm",
            "created_at": sent_at,
            "fan_intent": None,
            "mirror_tier": None,
            "post_type_classification": None,
            "token_usage_input": None,
            "token_usage_output": None,
            "model": None,
            "_backfilled": True,
            "_source": "dm_queue",
        }

        print(f"  {label}queued DM → @{handle} ({trigger}, {sent_at})")
        if not dry_run:
            db.collection("engagement_events").add(event)
        total += 1

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Done — {total} event(s) {'would be' if dry_run else ''} written to engagement_events.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)
