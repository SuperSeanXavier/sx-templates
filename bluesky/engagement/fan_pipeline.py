"""
Fan engagement pipeline — DM queue management and inbound DM polling.

queue_dm()           — write a DM to the Firestore outbound queue (follows)
send_engagement_dm() — send a like/repost DM immediately (called from poll-notifications)
process_dm_queue()   — batch executor: dequeue, generate, send, stagger sends (follows)
poll_inbound_dms()   — check active conversations for fan replies and respond
"""
import os
import random
import time
from datetime import datetime, timezone, timedelta, date

from google.cloud.firestore_v1.base_query import FieldFilter as _filter
from bluesky.shared.firestore_client import db
from bluesky.shared.rate_limiter import (
    check_write, seconds_until_next_write, RateLimitError,
)
from bluesky.reply.dm_generator import (
    generate_like_dm,
    generate_repost_dm,
    generate_creator_repost_dm,
    generate_themed_repost_dm,
    generate_studio_repost_dm,
    generate_conversation_reply,
    generate_dm_subscriber_reply,
    generate_dm_funnel_reply,
)
from bluesky.reply.reply_generator import classify_fan_intent, classify_subscriber_mention
from bluesky.engagement.handoff import check_handoff_triggers, flag_handoff

DAILY_DM_CAP = int(os.environ.get("DAILY_DM_CAP", "50"))
PRIORITY_MAP = {"follow": 3, "repost": 2, "like": 1}


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def queue_dm(fan_handle, fan_did, trigger_type, post_context, user_type, interaction_at=None):
    """Add an outbound DM to the Firestore queue."""
    priority = PRIORITY_MAP.get(trigger_type, 1)
    now = datetime.now(timezone.utc).isoformat()
    db.collection("dm_queue").add({
        "fan_handle": fan_handle,
        "fan_did": fan_did,
        "trigger_type": trigger_type,
        "post_context": post_context[:300],
        "user_type": user_type,
        "priority": priority,
        "status": "pending",
        "interaction_at": interaction_at or now,
        "created_at": now,
        "sent_at": None,
        "sent_date": None,
        "skip_reason": None,
    })
    print(f"  [queued] @{fan_handle} ({trigger_type}, priority {priority})")


# ---------------------------------------------------------------------------
# Immediate send (likes / reposts from poll-notifications)
# ---------------------------------------------------------------------------

def send_engagement_dm(client, handle, fan_did, trigger_type, post_context, user_type, brand_voice, dry_run=False):
    """
    Generate and send a like/repost thank-you DM immediately.
    Called directly from poll-notifications — no queue, no stagger.
    Follows still go through queue_dm / process_dm_queue.

    Returns: "sent" | "skipped" | "error"
    """
    if _already_dmed(handle):
        print(f"  [skip] @{handle} already has an outreach conversation")
        return "skipped"

    try:
        convo = client.get_dm_convo_status(handle)
    except Exception as e:
        print(f"  [warn] convo fetch failed for @{handle}: {e}")
        return "error"

    convo_id = convo["convo_id"]
    continuation = convo["last_their_message"] if convo["last_sender"] == "them" else None

    try:
        if user_type == "studio":
            dm_text = generate_studio_repost_dm(handle, post_context, brand_voice)
        elif user_type == "themed":
            dm_text = generate_themed_repost_dm(handle, post_context, brand_voice, continuation)
        elif user_type == "creator":
            dm_text = generate_creator_repost_dm(handle, post_context, brand_voice, continuation)
        elif trigger_type == "repost":
            dm_text = generate_repost_dm(handle, post_context, brand_voice, continuation)
        else:
            dm_text = generate_like_dm(handle, post_context, brand_voice, continuation)
    except Exception as e:
        print(f"  [warn] DM generation failed for @{handle}: {e}")
        return "error"

    print(f"  [dm → @{handle}] {dm_text[:120]}")

    if dry_run:
        print(f"  [dry-run]")
        return "sent"

    wait = seconds_until_next_write()
    if wait > 0:
        print(f"  [rate] waiting {wait:.0f}s for write window...")
        time.sleep(wait)

    try:
        check_write("create")
        client.send_dm(convo_id, dm_text)
        now = datetime.now(timezone.utc).isoformat()

        db.collection("conversations").document(handle).set({
            "convo_id": convo_id,
            "fan_handle": handle,
            "fan_did": fan_did,
            "stage": "warm",
            "human_handoff": False,
            "handoff_reason": None,
            "trigger_context": trigger_type,
            "created_at": now,
            "last_message_at": now,
            "last_fan_message": None,
        }, merge=True)

        db.collection("conversations").document(handle) \
          .collection("messages").add({
            "role": "assistant",
            "content": dm_text,
            "timestamp": now,
        })

        print(f"  [sent]")
        return "sent"

    except RateLimitError as e:
        print(f"  [rate limit] {e}")
        return "error"
    except Exception as e:
        print(f"  [error] send failed for @{handle}: {e}")
        return "error"


# ---------------------------------------------------------------------------
# Batch executor
# ---------------------------------------------------------------------------

def _daily_sent_count():
    today = date.today().isoformat()
    docs = (
        db.collection("dm_queue")
        .where(filter=_filter("status", "==", "sent"))
        .where(filter=_filter("sent_date", "==", today))
        .stream()
    )
    return sum(1 for _ in docs)


def _already_dmed(fan_handle):
    """True if we've ever initiated outreach to this handle."""
    return db.collection("conversations").document(fan_handle).get().exists


def _resolve_dm_discount():
    """Return discount dict if env vars are set, else None."""
    code = os.environ.get("FAN_DISCOUNT_CODE") or os.environ.get("DISCOUNT_OFFER")
    url = os.environ.get("FAN_DISCOUNT_URL_DM") or os.environ.get("FAN_DISCOUNT_URL_REPLY")
    if not code:
        return None
    return {"code": code, "url": url}


def _get_pending_batch(batch_size):
    docs = (
        db.collection("dm_queue")
        .where(filter=_filter("status", "==", "pending"))
        .stream()
    )
    items = [{"id": d.id, **d.to_dict()} for d in docs]
    items.sort(key=lambda x: (-x["priority"], x["created_at"]))
    return items[:batch_size]


def process_dm_queue_eligibility():
    """
    Lightweight pre-screen of the pending dm_queue (Firestore only, no API calls).
    Marks items as skipped if the handle has already been DMed via outreach.
    Run every 2 hours to keep the queue clean before the batch executor runs.
    """
    docs = (
        db.collection("dm_queue")
        .where(filter=_filter("status", "==", "pending"))
        .stream()
    )
    items = [{"id": d.id, **d.to_dict()} for d in docs]
    print(f"[dm_eligibility] checking {len(items)} pending item(s)...")
    skipped = 0
    for item in items:
        handle = item.get("fan_handle", "")
        trigger = item.get("trigger_type", "")

        if _already_dmed(handle):
            db.collection("dm_queue").document(item["id"]).update({
                "status": "skipped",
                "skip_reason": "already_dmed",
            })
            skipped += 1
            print(f"  [skip] @{handle} already has an outreach conversation")
    print(f"[dm_eligibility] done — {skipped} item(s) skipped")
    return {
        "items_checked": len(items),
        "already_dmed": skipped,
    }


def process_dm_queue(client, brand_voice, batch_size=15, dry_run=False):
    """
    Dequeue up to batch_size pending DMs, generate, and send them.
    Enforces: daily cap of 50, 4-min global write window, 8–20 min inter-send stagger,
    and never-DM-same-handle-twice.
    """
    daily_count = _daily_sent_count()
    if daily_count >= DAILY_DM_CAP:
        print(f"[dm_queue] daily cap reached ({daily_count}/{DAILY_DM_CAP})")
        return {
            "sent": 0,
            "skipped": 0,
            "outcome": "cap_hit",
            "cap_used": daily_count,
            "cap_remaining": 0,
            "by_user_type": {},
            "by_trigger": {},
        }

    batch = _get_pending_batch(batch_size)
    print(f"[dm_queue] {len(batch)} pending DM(s), {daily_count} sent today")

    sent = 0
    skipped = 0
    by_user_type: dict = {}
    by_trigger: dict = {}

    for i, item in enumerate(batch):
        doc_ref = db.collection("dm_queue").document(item["id"])
        handle = item["fan_handle"]

        if daily_count + sent >= DAILY_DM_CAP:
            print(f"  [dm_queue] daily cap reached mid-batch")
            break

        # Never DM same handle twice through outreach
        if _already_dmed(handle):
            print(f"  [skip] @{handle} already has an outreach conversation")
            doc_ref.update({"status": "skipped", "skip_reason": "already_dmed"})
            skipped += 1
            continue

        # Respect global 4-min write window
        wait = seconds_until_next_write()
        if wait > 0:
            print(f"  [rate] waiting {wait:.0f}s for write window...")
            if not dry_run:
                time.sleep(wait)

        # Fetch live convo (get convo_id, check if they've replied)
        try:
            convo = client.get_dm_convo_status(handle)
        except Exception as e:
            print(f"  [warn] convo fetch failed for @{handle}: {e}")
            doc_ref.update({"status": "skipped", "skip_reason": "convo_fetch_failed"})
            skipped += 1
            continue

        convo_id = convo["convo_id"]
        continuation = convo["last_their_message"] if convo["last_sender"] == "them" else None

        # Generate DM text
        trigger = item["trigger_type"]
        user_type = item.get("user_type", "fan")
        post_context = item.get("post_context", "")

        try:
            if user_type == "studio":
                dm_text = generate_studio_repost_dm(handle, post_context, brand_voice)
            elif user_type == "themed":
                dm_text = generate_themed_repost_dm(handle, post_context, brand_voice, continuation)
            elif user_type == "creator":
                dm_text = generate_creator_repost_dm(handle, post_context, brand_voice, continuation)
            elif trigger == "repost":
                dm_text = generate_repost_dm(handle, post_context, brand_voice, continuation)
            else:
                dm_text = generate_like_dm(handle, post_context, brand_voice, continuation)
        except Exception as e:
            print(f"  [warn] DM generation failed for @{handle}: {e}")
            doc_ref.update({"status": "skipped", "skip_reason": "generation_failed"})
            skipped += 1
            continue

        print(f"  [dm → @{handle}] {dm_text[:120]}")

        if not dry_run:
            try:
                check_write("create")
                client.send_dm(convo_id, dm_text)
                now = datetime.now(timezone.utc).isoformat()
                today = date.today().isoformat()

                doc_ref.update({
                    "status": "sent",
                    "sent_at": now,
                    "sent_date": today,
                    "convo_id": convo_id,
                })

                # Create conversation record
                db.collection("conversations").document(handle).set({
                    "convo_id": convo_id,
                    "fan_handle": handle,
                    "fan_did": item.get("fan_did", ""),
                    "stage": "warm",
                    "human_handoff": False,
                    "handoff_reason": None,
                    "trigger_context": trigger,
                    "created_at": now,
                    "last_message_at": now,
                    "last_fan_message": None,
                }, merge=True)

                # Log outbound message
                db.collection("conversations").document(handle) \
                  .collection("messages").add({
                    "role": "assistant",
                    "content": dm_text,
                    "timestamp": now,
                })

                sent += 1
                by_user_type[user_type] = by_user_type.get(user_type, 0) + 1
                by_trigger[trigger] = by_trigger.get(trigger, 0) + 1
                print(f"  [sent]")

                # Stagger: 8–20 min between sends (skip sleep after last item)
                if i < len(batch) - 1 and daily_count + sent < DAILY_DM_CAP:
                    stagger = random.uniform(480, 1200)
                    print(f"  [stagger] {stagger / 60:.1f} min until next send...")
                    time.sleep(stagger)

            except RateLimitError as e:
                print(f"  [rate limit] {e}")
                break
            except Exception as e:
                print(f"  [error] send failed for @{handle}: {e}")
                doc_ref.update({"status": "skipped", "skip_reason": f"send_failed"})
                skipped += 1
        else:
            print(f"  [dry-run]")
            sent += 1
            by_user_type[user_type] = by_user_type.get(user_type, 0) + 1
            by_trigger[trigger] = by_trigger.get(trigger, 0) + 1

    print(f"[dm_queue] done — sent: {sent}, skipped: {skipped}")
    return {
        "sent": sent,
        "skipped": skipped,
        "outcome": "ok",
        "cap_used": daily_count + sent,
        "cap_remaining": max(0, DAILY_DM_CAP - daily_count - sent),
        "by_user_type": by_user_type,
        "by_trigger": by_trigger,
    }


# ---------------------------------------------------------------------------
# Inbound DM polling
# ---------------------------------------------------------------------------

def poll_inbound_dms(client, brand_voice, dry_run=False):
    """
    Check for new fan DMs and reply.

    Uses chat.bsky.convo.listConvos to find conversations with unread_count > 0,
    rather than streaming all Firestore conversations. This means:
      - Cost is O(total_convos / 100) API calls, not O(active_convos * 3)
      - Only conversations with actual new messages are processed
      - human_handoff is checked per conversation after an unread message is found,
        not as a database filter — handed-off conversations stay unread in Bluesky
        so a human operator can see them

    Only replies to conversations we initiated (have a Firestore record for).
    Unsolicited inbound DMs are ignored.
    """
    print("[inbound_dms] checking for unread DMs via listConvos...")

    unread = []
    cursor = None

    while True:
        try:
            resp = client.list_convos(limit=100, cursor=cursor)
        except Exception as e:
            print(f"[inbound_dms] listConvos failed: {e}")
            return

        convos = getattr(resp, "convos", []) or []
        if not convos:
            break

        for convo in convos:
            if getattr(convo, "unread_count", 0) > 0:
                unread.append(convo)

        cursor = getattr(resp, "cursor", None)
        # Stop paginating once we reach convos with no unread messages —
        # listConvos is sorted by most recent activity, so unread convos cluster at the top.
        # Once a full page has no unread entries, earlier pages won't either.
        if not any(getattr(c, "unread_count", 0) > 0 for c in convos):
            break
        if not cursor:
            break

    print(f"[inbound_dms] {len(unread)} conversation(s) with unread messages")

    replies_sent = 0
    handoffs_triggered = 0
    skipped_handoff_active = 0
    skipped_unsolicited = 0
    skipped_no_new_content = 0

    for convo in unread:
        convo_id = getattr(convo, "id", None)
        if not convo_id:
            continue

        # Identify the fan (the member who isn't us)
        members = getattr(convo, "members", []) or []
        fan_member = next(
            (m for m in members if getattr(m, "did", None) != client._my_did),
            None,
        )
        if not fan_member:
            continue

        handle = getattr(fan_member, "handle", None)
        if not handle:
            continue

        # Extract message text and sender from lastMessage
        last_msg = getattr(convo, "last_message", None)
        if not last_msg:
            continue

        sender_did = getattr(getattr(last_msg, "sender", None), "did", None)
        if sender_did == client._my_did:
            # We sent the last message — nothing to reply to
            continue

        their_message = getattr(last_msg, "text", None)
        if not their_message:
            continue

        # Look up our Firestore record — only reply to conversations we initiated
        convo_doc = db.collection("conversations").document(handle).get()
        if not convo_doc.exists:
            print(f"  [skip] @{handle} — no outreach record, ignoring unsolicited DM")
            skipped_unsolicited += 1
            continue

        convo_data = convo_doc.to_dict()

        # human_handoff guard: leave the message unread so a human operator sees it
        if convo_data.get("human_handoff", False):
            print(f"  [skip] @{handle} — handed off, leaving unread for human")
            skipped_handoff_active += 1
            continue

        # Skip if we already handled this exact message
        if their_message == convo_data.get("last_fan_message"):
            skipped_no_new_content += 1
            continue

        print(f"  [inbound] @{handle}: {their_message[:80]}")

        # Fetch message history from Firestore for reply context
        history_docs = (
            db.collection("conversations").document(handle)
            .collection("messages")
            .order_by("timestamp")
            .stream()
        )
        history = [
            {"role": d.to_dict()["role"], "content": d.to_dict()["content"]}
            for d in history_docs
        ]

        exchange_count = sum(1 for h in history if h["role"] == "user")

        # Handoff trigger check before generating a reply
        should_handoff, reason = check_handoff_triggers(their_message, exchange_count)
        if should_handoff:
            flag_handoff(handle, reason)
            db.collection("conversations").document(handle).update({
                "last_fan_message": their_message,
            })
            handoffs_triggered += 1
            continue

        # Classify the fan's message to route to the right generator
        is_subscriber = classify_subscriber_mention(their_message)
        intent = classify_fan_intent(their_message)

        discount_sent = None
        try:
            if is_subscriber:
                dm_reply = generate_dm_subscriber_reply(handle, their_message, history, brand_voice)
                new_stage = "subscriber"
            elif intent in ("buying_signal", "curious"):
                already_discounted = convo_data.get("discount_sent", False)
                discount = _resolve_dm_discount() if not already_discounted else None
                dm_reply = generate_dm_funnel_reply(handle, their_message, history, brand_voice, discount=discount)
                discount_sent = discount
                new_stage = "converted" if discount else "engaged"
            else:
                # Casual/neutral — keep engaging; suppress built-in CTA (intent gate owns that)
                dm_reply = generate_conversation_reply(handle, their_message, history, brand_voice, exchange_count=0)
                new_stage = "engaged"
        except Exception as e:
            print(f"  [warn] reply generation failed for @{handle}: {e}")
            continue

        print(f"  [reply → @{handle}] {dm_reply[:120]}")

        if not dry_run:
            wait = seconds_until_next_write()
            if wait > 0:
                print(f"  [rate] waiting {wait:.0f}s...")
                time.sleep(wait)
            try:
                check_write("create")
                client.send_dm(convo_id, dm_reply)
                now = datetime.now(timezone.utc).isoformat()

                update_payload = {
                    "stage": new_stage,
                    "last_message_at": now,
                    "last_fan_message": their_message,
                }
                if discount_sent:
                    update_payload["discount_sent"] = True
                db.collection("conversations").document(handle).update(update_payload)

                dt = datetime.now(timezone.utc)
                msgs = db.collection("conversations").document(handle).collection("messages")
                msgs.add({"role": "user", "content": their_message, "timestamp": dt.isoformat()})
                msgs.add({"role": "assistant", "content": dm_reply, "timestamp": (dt + timedelta(microseconds=1)).isoformat()})
                print(f"  [reply sent]")

            except RateLimitError as e:
                print(f"  [rate limit] {e}")
            except Exception as e:
                print(f"  [error] reply failed for @{handle}: {e}")
            else:
                replies_sent += 1
        else:
            print(f"  [dry-run]")
            dry_update = {"last_fan_message": their_message, "stage": new_stage}
            if discount_sent:
                dry_update["discount_sent"] = True
            db.collection("conversations").document(handle).update(dry_update)
            replies_sent += 1

    return {
        "convos_with_unread": len(unread),
        "replies_sent": replies_sent,
        "handoffs_triggered": handoffs_triggered,
        "skipped_handoff_active": skipped_handoff_active,
        "skipped_unsolicited": skipped_unsolicited,
        "skipped_no_new_content": skipped_no_new_content,
    }
