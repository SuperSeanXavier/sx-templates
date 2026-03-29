"""
Compare two insights approaches:
  A) LLM-generated from real data (honest context, no hallucination)
  B) Python-computed directly from Firestore

Run from project root:
    python scripts/test_insights.py
"""
import os
import sys
import json
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bluesky.shared.firestore_client import db
import anthropic

# ---------------------------------------------------------------------------
# Pull raw data
# ---------------------------------------------------------------------------

def _utc_now():
    return datetime.now(timezone.utc)

def fetch_data(hours=24):
    start = _utc_now() - timedelta(hours=hours)
    events = [
        d.to_dict()
        for d in db.collection("engagement_events")
        .where("created_at", ">=", start.isoformat())
        .stream()
    ]

    settings_doc = db.collection("_system").document("settings").get()
    settings = settings_doc.to_dict() or {} if settings_doc.exists else {}
    discount_cap = int(settings.get("caps", {}).get("max_discounts_per_day", 5))

    return events, discount_cap


# ---------------------------------------------------------------------------
# Approach A: LLM-generated from honest, complete context
# ---------------------------------------------------------------------------

def approach_a(events, discount_cap):
    fan_replies_in  = sum(1 for e in events if e.get("type") == "reply" and e.get("direction") == "inbound")
    fan_replies_out = sum(1 for e in events if e.get("type") == "reply" and e.get("direction") == "outbound" and e.get("user_type") == "fan")
    dms_sent        = sum(1 for e in events if e.get("type") == "dm"    and e.get("direction") == "outbound")
    dm_pulls        = sum(1 for e in events if e.get("reply_type") == "fan_dm_pull")
    buying_signals  = sum(1 for e in events if e.get("fan_intent") in ("buying_signal", "curious"))
    handoffs        = sum(1 for e in events if e.get("interaction_subtype") == "handoff")
    sub_guard       = sum(1 for e in events if e.get("reply_type") == "subscriber_warmth")
    discounts_sent  = sum(1 for e in events if e.get("interaction_subtype") == "discount_sent")
    follows_in      = sum(1 for e in events if e.get("type") == "follow" and e.get("direction") == "inbound")

    dm_pull_rate = round(dm_pulls / fan_replies_out * 100) if fan_replies_out else 0
    discount_pct = round(discounts_sent / discount_cap * 100) if discount_cap else 0

    context = (
        f"Last 24h stats: "
        f"{fan_replies_in} inbound fan replies, "
        f"{fan_replies_out} outbound fan replies, "
        f"{dm_pulls} DM pulls sent (DM pull rate: {dm_pull_rate}%), "
        f"{buying_signals} buying/curious signals, "
        f"{dms_sent} total DMs sent, "
        f"{handoffs} handoffs to human, "
        f"{sub_guard} subscriber guard fires (funnel skipped for possible existing subscribers), "
        f"{discounts_sent} discounts sent out of {discount_cap} daily cap ({discount_pct}% of cap used), "
        f"{follows_in} new followers."
    )

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=250,
        messages=[{
            "role": "user",
            "content": (
                "You are an analytics assistant for a Bluesky creator bot. "
                "Write 2-3 short insights (each 1 sentence, pipe-separated |) based ONLY on the numbers provided. "
                "Do not invent any numbers or details not in the context. "
                "Be specific and concise.\n\n"
                f"{context}"
            ),
        }],
    )
    return [s.strip() for s in msg.content[0].text.strip().split("|") if s.strip()], context


# ---------------------------------------------------------------------------
# Approach B: Python-computed — no LLM involved
# ---------------------------------------------------------------------------

def approach_b(events, discount_cap):
    fan_replies_out = sum(1 for e in events if e.get("type") == "reply" and e.get("direction") == "outbound" and e.get("user_type") == "fan")
    dm_pulls        = sum(1 for e in events if e.get("reply_type") == "fan_dm_pull")
    buying_signals  = sum(1 for e in events if e.get("fan_intent") in ("buying_signal", "curious"))
    handoffs        = sum(1 for e in events if e.get("interaction_subtype") == "handoff")
    sub_guard       = sum(1 for e in events if e.get("reply_type") == "subscriber_warmth")
    discounts_sent  = sum(1 for e in events if e.get("interaction_subtype") == "discount_sent")
    dms_sent        = sum(1 for e in events if e.get("type") == "dm" and e.get("direction") == "outbound")
    follows_in      = sum(1 for e in events if e.get("type") == "follow" and e.get("direction") == "inbound")

    insights = []

    # DM pull rate
    if fan_replies_out > 0:
        rate = round(dm_pulls / fan_replies_out * 100)
        insights.append(f"DM pull rate: {dm_pulls} of {fan_replies_out} fan replies converted ({rate}%).")
    elif fan_replies_out == 0:
        insights.append("No fan replies in the last 24h.")

    # Buying signals
    if buying_signals > 0:
        insights.append(f"{buying_signals} buying or curious signal{'s' if buying_signals != 1 else ''} detected in the last 24h.")

    # Subscriber guard
    if sub_guard > 0:
        insights.append(f"Funnel skipped on {sub_guard} possible subscriber{'s' if sub_guard != 1 else ''}.")

    # Discounts
    if discount_cap > 0:
        pct = round(discounts_sent / discount_cap * 100)
        insights.append(f"Discount cap at {pct}% — {discounts_sent} of {discount_cap} sent today.")

    # Handoffs
    if handoffs > 0:
        insights.append(f"{handoffs} conversation{'s' if handoffs != 1 else ''} handed off to you for a personal reply.")

    # DMs sent
    if dms_sent > 0:
        insights.append(f"{dms_sent} outbound DM{'s' if dms_sent != 1 else ''} sent.")

    # New followers
    if follows_in > 0:
        insights.append(f"{follows_in} new follower{'s' if follows_in != 1 else ''} in the last 24h.")

    if not insights:
        insights.append("No activity in the last 24h.")

    return insights


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Fetching data from Firestore...")
    events, discount_cap = fetch_data()
    print(f"  {len(events)} events found, discount cap: {discount_cap}\n")

    print("=" * 60)
    print("APPROACH A — LLM-written from real data")
    print("=" * 60)
    insights_a, context = approach_a(events, discount_cap)
    print(f"\nContext sent to LLM:\n  {context}\n")
    print("Insights:")
    for i, ins in enumerate(insights_a, 1):
        print(f"  {i}. {ins}")

    print()
    print("=" * 60)
    print("APPROACH B — Python-computed, no LLM")
    print("=" * 60)
    insights_b = approach_b(events, discount_cap)
    print("\nInsights:")
    for i, ins in enumerate(insights_b, 1):
        print(f"  {i}. {ins}")
