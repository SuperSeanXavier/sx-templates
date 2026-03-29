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
from bluesky.shared.cost_calculator import write_cost_event
from bluesky.shared.rate_limiter import (
    check_write, seconds_until_next_write, RateLimitError,
    check_dm_write, seconds_until_next_dm_write, is_active_hours,
)
from zoneinfo import ZoneInfo
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
PRIORITY_MAP = {"follow": 3, "repost": 2, "like": 1, "comment_exchange": 2}
DASHBOARD_LOOKBACK_DAYS = 30  # matches the maximum time range on the dashboard
ENGAGEMENT_DM_RECENCY_HOURS = 1  # skip engagement DMs if interaction is older than this

_ACTIVE_TZ = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def queue_dm(fan_handle, fan_did, trigger_type, post_context, user_type,
             interaction_at=None, post_created_at=None):
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
        "post_created_at": post_created_at or now,
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

    wait = seconds_until_next_dm_write()
    if wait > 0:
        print(f"  [dm rate] waiting {wait:.0f}s for DM write window...")
        time.sleep(wait)

    try:
        check_dm_write()
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

        try:
            db.collection("engagement_events").add({
                "type": "dm",
                "direction": "outbound",
                "handle": handle,
                "post_uri": None,
                "reply_type": "dm_outreach",
                "interaction_subtype": f"{trigger_type}_trigger",
                "user_type": user_type,
                "created_at": now,
            })
        except Exception:
            pass
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

                try:
                    db.collection("engagement_events").add({
                        "type": "dm",
                        "direction": "outbound",
                        "handle": handle,
                        "post_uri": None,
                        "reply_type": "dm_outreach",
                        "interaction_subtype": f"{trigger}_trigger",
                        "user_type": user_type,
                        "created_at": now,
                    })
                except Exception:
                    pass

                sent += 1
                by_user_type[user_type] = by_user_type.get(user_type, 0) + 1
                by_trigger[trigger] = by_trigger.get(trigger, 0) + 1
                print(f"  [sent]")

                # Stagger: 5–15 min between sends (skip sleep after last item)
                # 10 items × 300s min = 3000s — fits within the 3600s CF timeout
                if i < len(batch) - 1 and daily_count + sent < DAILY_DM_CAP:
                    stagger = random.uniform(300, 900)
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
# Engagement DM queue executor (likes / reposts / comment_exchange)
# ---------------------------------------------------------------------------

def _in_inbound_dm_burst_window():
    """
    True if current Pacific time is within 60 min of a 3-hour boundary (0, 3, 6, 9, 12...).
    Used to gate poll_inbound_dms — only process inbound DMs during burst windows.
    """
    now_local = datetime.now(_ACTIVE_TZ)
    minutes_since_boundary = (now_local.hour % 3) * 60 + now_local.minute
    return minutes_since_boundary < 60


def execute_engagement_dm_queue(client, brand_voice, batch_size=10, dry_run=False):
    """
    Drain pending like/repost/comment_exchange DMs.
    Only processes items where interaction_at is within the last ENGAGEMENT_DM_RECENCY_HOURS.
    Sorted by interaction_at DESC (most recent engagement first).
    Uses the DM-specific 60s write window (independent of the 4-min public write window).
    """
    if not is_active_hours():
        print("[engagement_dms] outside active hours — skipping")
        return {"skipped": "outside_active_hours", "sent": 0}

    engagement_types = {"like", "repost", "comment_exchange"}
    now = datetime.now(timezone.utc)
    recency_cutoff = (now - timedelta(hours=ENGAGEMENT_DM_RECENCY_HOURS)).isoformat()

    docs = (
        db.collection("dm_queue")
        .where(filter=_filter("status", "==", "pending"))
        .stream()
    )
    items = [
        {"id": d.id, **d.to_dict()} for d in docs
        if d.to_dict().get("trigger_type") in engagement_types
    ]
    # Most recent engagement first
    items.sort(key=lambda x: x.get("interaction_at", ""), reverse=True)
    items = items[:batch_size]

    print(f"[engagement_dms] {len(items)} pending engagement DM(s)")

    sent = 0
    skipped_too_old = 0
    skipped_already_dmed = 0
    skipped_other = 0
    first_send = True

    for item in items:
        doc_ref = db.collection("dm_queue").document(item["id"])
        handle = item["fan_handle"]
        interaction_at = item.get("interaction_at", "")

        # Recency gate: skip if engagement is older than 1hr
        if interaction_at and interaction_at < recency_cutoff:
            doc_ref.update({"status": "skipped", "skip_reason": "engagement_too_old"})
            skipped_too_old += 1
            print(f"  [skip] @{handle} engagement too old ({interaction_at[:19]})")
            continue

        if _already_dmed(handle):
            doc_ref.update({"status": "skipped", "skip_reason": "already_dmed"})
            skipped_already_dmed += 1
            print(f"  [skip] @{handle} already has an outreach conversation")
            continue

        # Human pacing: 90–600s between sends (skip before the first send)
        if not first_send and not dry_run:
            stagger = random.uniform(90, 600)
            print(f"  [stagger] {stagger:.0f}s before next DM...")
            time.sleep(stagger)

        trigger = item.get("trigger_type", "like")
        user_type = item.get("user_type", "fan")
        post_context = item.get("post_context", "")
        fan_did = item.get("fan_did", "")

        result = send_engagement_dm(
            client, handle, fan_did, trigger,
            post_context, user_type, brand_voice, dry_run=dry_run,
        )

        if result == "sent":
            doc_ref.update({
                "status": "sent",
                "sent_at": now.isoformat(),
                "sent_date": now.date().isoformat(),
            })
            sent += 1
            first_send = False
        elif result == "skipped":
            doc_ref.update({"status": "skipped", "skip_reason": "already_dmed"})
            skipped_already_dmed += 1
        else:
            skipped_other += 1

    print(f"[engagement_dms] done — sent: {sent}, too_old: {skipped_too_old}, "
          f"already_dmed: {skipped_already_dmed}, other: {skipped_other}")
    return {
        "sent": sent,
        "skipped_too_old": skipped_too_old,
        "skipped_already_dmed": skipped_already_dmed,
        "skipped_other": skipped_other,
    }


# ---------------------------------------------------------------------------
# Inbound DM polling
# ---------------------------------------------------------------------------

def _snapshot_my_posts(client):
    """
    Write engagement_events for any new posts published by this account.
    Paginates get_author_feed until all posts within a 30-day lookback are
    captured, matching the maximum dashboard time range.
    Called at the start of each poll_inbound_dms cycle.
    """
    handle = os.environ.get("BLUESKY_HANDLE", "")
    if not handle:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=DASHBOARD_LOOKBACK_DAYS)

    # Build set of already-tracked post URIs
    existing = set()
    try:
        for doc in (
            db.collection("engagement_events")
            .where(filter=_filter("type", "==", "post"))
            .stream()
        ):
            uri = doc.to_dict().get("post_uri")
            if uri:
                existing.add(uri)
    except Exception as e:
        print(f"[snapshot_posts] existing URI fetch failed: {e}")

    new_count = 0
    cursor = None

    while True:
        try:
            resp = client.get_author_feed(handle, limit=100, cursor=cursor)
        except Exception as e:
            print(f"[snapshot_posts] feed fetch failed: {e}")
            break

        feed = getattr(resp, "feed", []) or []
        if not feed:
            break

        done = False
        for feed_item in feed:
            post = getattr(feed_item, "post", None)
            if not post:
                continue
            uri = getattr(post, "uri", None)
            if not uri:
                continue

            record = getattr(post, "record", None)
            created_at = getattr(record, "created_at", None) or datetime.now(timezone.utc).isoformat()

            # Stop paginating once posts fall outside the 30-day window
            try:
                post_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if post_dt < cutoff:
                    done = True
                    break
            except Exception:
                pass

            if uri in existing:
                continue

            text = getattr(record, "text", "") or ""
            try:
                db.collection("engagement_events").add({
                    "type": "post",
                    "direction": "outbound",
                    "handle": handle,
                    "post_uri": uri,
                    "post_text": text[:300],
                    "created_at": created_at,
                })
                existing.add(uri)
                new_count += 1
            except Exception as e:
                print(f"[snapshot_posts] write failed for {uri}: {e}")

        cursor = getattr(resp, "cursor", None)
        if done or not cursor:
            break

    if new_count:
        print(f"[snapshot_posts] wrote {new_count} new post event(s)")
    return new_count


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
    if not is_active_hours():
        print("[inbound_dms] outside active hours — skipping")
        return {"skipped": "outside_active_hours"}

    if not _in_inbound_dm_burst_window():
        print("[inbound_dms] outside burst window — skipping")
        return {"skipped": "outside_burst_window"}

    _snapshot_my_posts(client)

    # --- Send any pending human operator replies first ---
    try:
        for pdoc in db.collection("conversations").where(filter=_filter("has_pending_manual_reply", "==", True)).stream():
            pdata = pdoc.to_dict() or {}
            phandle = pdata.get("fan_handle", pdoc.id)
            pending_reply = pdata.get("pending_manual_reply", "")
            pconvo_id = pdata.get("convo_id")
            if not pending_reply or not pconvo_id:
                continue
            print(f"  [human reply → @{phandle}] {pending_reply[:80]}")
            if not dry_run:
                try:
                    wait = seconds_until_next_write()
                    if wait > 0:
                        time.sleep(wait)
                    check_write("create")
                    client.send_dm(pconvo_id, pending_reply)
                    now = datetime.now(timezone.utc).isoformat()
                    pdoc.reference.update({
                        "pending_manual_reply": None,
                        "has_pending_manual_reply": False,
                        "last_message_at": now,
                    })
                    pdoc.reference.collection("messages").add({
                        "role": "assistant",
                        "content": pending_reply,
                        "timestamp": now,
                    })
                    print(f"  [human reply sent]")
                except RateLimitError as e:
                    print(f"  [rate limit] {e}")
                except Exception as e:
                    print(f"  [error] human reply failed for @{phandle}: {e}")
    except Exception as e:
        print(f"[warn] pending reply check failed: {e}")

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
        already_discounted = convo_data.get("discount_sent", False)

        discount_sent = None
        try:
            if is_subscriber:
                # Subscriber guard always wins — warm thanks, no pitch
                dm_reply = generate_dm_subscriber_reply(handle, their_message, history, brand_voice)
                new_stage = "subscriber"
            elif exchange_count == 0 and not already_discounted:
                # First ever reply to our initiation DM — offer discount immediately regardless of intent
                discount = _resolve_dm_discount()
                dm_reply = generate_dm_funnel_reply(handle, their_message, history, brand_voice, discount=discount)
                discount_sent = discount
                new_stage = "converted" if discount else "engaged"
            elif intent in ("buying_signal", "curious"):
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
                    update_payload["discount_sent_at"] = now
                db.collection("conversations").document(handle).update(update_payload)

                dt = datetime.now(timezone.utc)
                msgs = db.collection("conversations").document(handle).collection("messages")
                msgs.add({"role": "user", "content": their_message, "timestamp": dt.isoformat()})
                msgs.add({"role": "assistant", "content": dm_reply, "timestamp": (dt + timedelta(microseconds=1)).isoformat()})

                try:
                    db.collection("engagement_events").add({
                        "type": "reply",
                        "direction": "inbound",
                        "handle": handle,
                        "post_uri": None,
                        "fan_intent": intent,
                        "created_at": dt.isoformat(),
                    })
                except Exception:
                    pass

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
