"""
Bluesky reply bot — main polling loop.

Usage:
    python bluesky/reply/poller.py --dry-run --once   # generate + print, no post
    python bluesky/reply/poller.py --once             # one real cycle
    python bluesky/reply/poller.py --interval 60      # continuous, 60s poll
"""
import argparse
import hashlib
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import random

from bluesky.shared.bluesky_client import BlueskyClient
from bluesky.shared.firestore_client import db
from bluesky.shared.rate_limiter import check_write, seconds_until_next_write, RateLimitError, is_active_hours
from bluesky.shared.cost_calculator import write_cost_event
from bluesky.engagement.fan_pipeline import queue_dm, poll_inbound_dms
from bluesky.reply.reply_generator import (
    classify_post_type,
    classify_fan_intent,
    classify_subscriber_mention,
    generate_reply,
    generate_dm_pull_reply,
    generate_discount_pull_reply,
    generate_peer_reply,
    generate_subscriber_thanks,
    generate_studio_thanks,
    generate_themed_reply,
    simulate_fan_reply,
    load_brand_voice,
    PITCH_INTENT,
)
from bluesky.reply.state_manager import StateManager
from bluesky.reply.creator_classifier import classify_user, bot_score, BOT_SCORE_SKIP
from bluesky.reply.dm_manager import DMManager
from bluesky.reply.dm_generator import (
    generate_like_dm,
    generate_repost_dm,
    generate_creator_repost_dm,
    generate_themed_repost_dm,
    generate_studio_repost_dm,
)

# Dispatch table — add handlers here as scope expands (§ Notification Scope in CLAUDE.md)
HANDLERS = {
    "reply": "_handle_reply",  # active
    # "follow": handled inline in run_once via _handle_engagement
}


# ---------------------------------------------------------------------------
# Seen-events dedup (Firestore)
# ---------------------------------------------------------------------------

def _notif_id(uri):
    return hashlib.md5(uri.encode()).hexdigest()


def _is_seen(uri):
    return db.collection("seen_events").document(_notif_id(uri)).get().exists


def _mark_seen(uri):
    db.collection("seen_events").document(_notif_id(uri)).set({
        "uri": uri,
        "seen_at": datetime.now(timezone.utc).isoformat(),
    })


def _write_engagement_event(event_type, direction, handle, post_uri, **kwargs):
    """Write one engagement_events doc. Silently no-ops on error."""
    try:
        doc = {
            "type": event_type,
            "direction": direction,
            "handle": handle,
            "post_uri": post_uri,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        doc.update({k: v for k, v in kwargs.items() if v is not None})
        db.collection("engagement_events").add(doc)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# DM eligibility
# ---------------------------------------------------------------------------

def _is_eligible_for_dm(profile):
    """
    Returns (True, None) if the account can receive outreach DMs.
    Returns (False, reason) if it should be skipped.
    """
    followers = getattr(profile, "followers_count", 0) or 0
    follows = getattr(profile, "follows_count", 0) or 0
    posts = getattr(profile, "posts_count", 0) or 0

    if followers <= 50:
        return False, f"only {followers} followers"
    if posts == 0:
        return False, "zero posts (bot)"
    if followers > 0 and follows / followers > 20:
        return False, f"follow ratio {follows}/{followers} > 20x (bot)"

    return True, None


def _classify_user(handle, client, dm_state):
    """
    Return (user_type, follower_count) for a handle.

    Uses dm_state cache when fresh (< CLASSIFICATION_TTL_DAYS days).
    Falls back to API + classifier and caches the result.
    """
    cached = dm_state.get_cached_user_type(handle)
    if cached:
        return cached
    try:
        profile = client.get_profile(handle)
        classification = classify_user(profile)
        dm_state.cache_user_type(handle, classification.user_type, classification.follower_count)
        return classification.user_type, classification.follower_count
    except Exception as e:
        print(f"  [warn] could not classify @{handle}: {e}")
        return "fan", 0



def _handle_reply(notif, client, state, dm_state, brand_voice, dry_run):
    handle = notif.author.handle
    post_uri = notif.uri

    if state.has_replied(post_uri):
        return "skipped_duplicate"

    if state.is_blocked(handle):
        print(f"  [skip] blocked: @{handle}")
        return "skipped_blocked"

    if state.is_paused_user(handle):
        print(f"  [skip] paused: @{handle}")
        return "skipped_paused"

    reply_text = notif.record.text

    # Record inbound fan reply
    if not dry_run:
        _write_engagement_event("reply", "inbound", handle, post_uri)

    # Classify user type (cached — avoids repeat API calls)
    user_type, follower_count = _classify_user(handle, client, dm_state)
    print(f"  [user type: {user_type}, {follower_count:,} followers]")

    def _post_reply(generated):
        if not dry_run:
            # Human-pacing delay — skip in Cloud Functions (K_SERVICE is set by Cloud Run)
            if not os.environ.get("K_SERVICE"):
                delay = random.uniform(90, 600)
                print(f"  [delay] {delay:.0f}s before posting...")
                time.sleep(delay)
            # Respect global 4-min write window shared with DMs and comments.
            # In Cloud Functions, skip the sleep — check_write raises RateLimitError
            # if the window isn't clear; the reply stays unseen and retries next invocation.
            if not os.environ.get("K_SERVICE"):
                wait = seconds_until_next_write()
                if wait > 0:
                    print(f"  [rate] waiting {wait:.0f}s for write window...")
                    time.sleep(wait)
            root = notif.record.reply.root
            parent = notif.record.reply.parent
            try:
                check_write("create")
                response = client.post_reply(
                    text=generated,
                    parent_uri=parent.uri, parent_cid=parent.cid,
                    root_uri=root.uri, root_cid=root.cid,
                )
                state.mark_replied(post_uri)
                state.add_my_reply(response.uri)
                print("  [posted]")
            except RateLimitError as e:
                print(f"  [rate limit] {e} — reply skipped")
        else:
            state.mark_replied(post_uri)
            print("  [dry-run, not posted]")

    # --- Studio: simple thanks, no routing ---
    if user_type == "studio":
        # Need post context for a relevant thanks
        try:
            root_uri = notif.record.reply.root.uri
            root_post = client.get_post(root_uri)
            root_text = root_post.record.text
        except Exception:
            root_text = ""
        generated = generate_studio_thanks(root_text, reply_text, handle, brand_voice)
        print(f"  → {generated}")
        _post_reply(generated)
        if not dry_run:
            _write_engagement_event("reply", "outbound", handle, post_uri, reply_type="studio_reply", user_type="studio")
        return "studio_reply"

    # --- Creator: peer register ---
    if user_type == "creator":
        print(f"  [creator, {follower_count:,} followers]")
        options, peer_intent = generate_peer_reply(reply_text, handle, follower_count, brand_voice)
        print(f"  [peer intent: {peer_intent}]")
        generated = random.choice(options)
        print(f"  → {generated}")
        if len(options) > 1:
            print(f"  [decline options: {len(options)} generated, picked one randomly]")
        _post_reply(generated)
        if not dry_run:
            _write_engagement_event("reply", "outbound", handle, post_uri, reply_type="peer_reply", interaction_subtype=peer_intent, user_type="creator")
        return "creator_reply"

    parent_uri = notif.record.reply.parent.uri
    root_uri = notif.record.reply.root.uri
    is_followup = state.is_my_reply(parent_uri)

    # Fetch parent post for conversational context
    try:
        parent_post = client.get_post(parent_uri)
        parent_text = parent_post.record.text
    except Exception as e:
        print(f"  [warn] could not fetch parent post: {e}")
        parent_text = "[unavailable]"

    # Fetch root post for post-type classification (may be same as parent)
    if root_uri == parent_uri:
        root_text = parent_text
    else:
        try:
            root_post = client.get_post(root_uri)
            root_text = root_post.record.text
        except Exception as e:
            print(f"  [warn] could not fetch root post: {e}")
            root_text = parent_text

    # --- Themed: playful, niche-aware reply ---
    if user_type == "themed":
        generated = generate_themed_reply(root_text, reply_text, handle, brand_voice)
        print(f"  → {generated}")
        _post_reply(generated)
        if not dry_run:
            _write_engagement_event("reply", "outbound", handle, post_uri, reply_type="themed_reply", user_type="themed")
        return "themed_reply"

    # Subscriber guard — existing members get a warm thank-you, no pitch, no discount
    if classify_subscriber_mention(reply_text):
        print(f"  [subscriber] @{handle} mentioned being already subscribed")
        generated = generate_subscriber_thanks(root_text, reply_text, handle, brand_voice)
        print(f"  → {generated}")
        _post_reply(generated)
        if not dry_run:
            _write_engagement_event("reply", "outbound", handle, post_uri, reply_type="subscriber_warmth", user_type="fan")
        return "subscriber_thanks"

    post_type = classify_post_type(root_text)
    intent = None  # set in follow-up branch when fan_intent is classified

    print(f"  @{handle}: {reply_text[:100]}")
    print(f"  [post type: {post_type}]")

    if random.random() > 0.75:
        # 25% → friendly reply, no nudge (preserves organic variety across all post types)
        generated = generate_reply(parent_text, reply_text, handle, brand_voice, nudge=False)
        action = "fan_casual"

    elif is_followup:
        intent = classify_fan_intent(reply_text)
        print(f"  [intent: {intent}]")

        if intent in PITCH_INTENT or state.at_max_depth(root_uri):
            used_pulls = state.get_dm_pulls(root_uri)
            generated = generate_dm_pull_reply(
                parent_text, reply_text, handle, brand_voice,
                used_pulls=used_pulls,
            )
            action = "fan_dm_pull"
            print("  [dm pull]")
        else:
            # After first nudge reply: use discount pull (up to 2x per thread),
            # then fall back to regular nudge.
            dm_pulls_count = len(state.get_dm_pulls(root_uri))
            if dm_pulls_count < 2:
                generated = generate_discount_pull_reply(root_text, reply_text, handle, brand_voice)
                action = "fan_discount_pull"
                print("  [discount pull]")
            else:
                generated = generate_reply(parent_text, reply_text, handle, brand_voice, nudge=True)
                action = "fan_nudge"

    else:
        # First reply to Sean's post — start the conversation with a nudging question
        generated = generate_reply(parent_text, reply_text, handle, brand_voice, nudge=True)
        action = "fan_nudge"

    print(f"  → {generated}")

    if dry_run:
        state.mark_replied(post_uri)
        if action == "fan_nudge" and not is_followup:
            _dry_run_simulate(handle, generated, root_text, brand_voice, root_uri, state)
        print("  [dry-run, not posted]")
        return action
    else:
        # Human-pacing delay (skip in Cloud Functions) + global write window
        if not os.environ.get("K_SERVICE"):
            delay = random.uniform(90, 600)
            print(f"  [delay] {delay:.0f}s before posting...")
            time.sleep(delay)
            wait = seconds_until_next_write()
            if wait > 0:
                print(f"  [rate] waiting {wait:.0f}s for write window...")
                time.sleep(wait)
        root = notif.record.reply.root
        parent = notif.record.reply.parent
        try:
            check_write("create")
            response = client.post_reply(
                text=generated,
                parent_uri=parent.uri,
                parent_cid=parent.cid,
                root_uri=root.uri,
                root_cid=root.cid,
            )
            state.mark_replied(post_uri)
            state.add_my_reply(response.uri)
            if is_followup and action != "fan_casual":
                if action == "fan_dm_pull":
                    state.add_dm_pull(root_uri, generated)
                    # Create Firestore conversations record so poll_inbound_dms
                    # recognises the fan when they DM after the public reply pull.
                    now = datetime.now(timezone.utc).isoformat()
                    db.collection("conversations").document(handle).set({
                        "stage": "dm_pull_sent",
                        "trigger_context": "reply_dm_pull",
                        "trigger_post_text": root_text[:300],
                        "fan_handle": handle,
                        "human_handoff": False,
                        "handoff_reason": None,
                        "created_at": now,
                        "last_message_at": now,
                        "last_fan_message": None,
                    }, merge=True)
                elif action == "fan_discount_pull":
                    state.add_dm_pull(root_uri, generated)
                    # Queue a proactive DM to this commenter — executor handles dedup
                    now_iso = datetime.now(timezone.utc).isoformat()
                    fan_did = getattr(notif.author, "did", None) or ""
                    queue_dm(
                        handle, fan_did, "comment_exchange", root_text, "fan",
                        interaction_at=now_iso,
                    )
                    print(f"  [queued comment_exchange DM → @{handle}]")
                else:
                    state.increment_depth(root_uri)
            _write_engagement_event(
                "reply", "outbound", handle, root_uri,
                reply_type=action,
                post_type_classification=post_type,
                fan_intent=intent if action in ("fan_dm_pull", "fan_discount_pull") else None,
                user_type="fan",
            )
            print("  [posted]")
            return action
        except RateLimitError as e:
            print(f"  [rate limit] {e} — reply skipped")
            return "skipped_rate_limit"


def _dry_run_simulate(handle, sean_reply, original_text, brand_voice, root_uri, state):
    """Simulate a fan follow-up and show what the DM pull reply would look like."""
    print("\n  [sim] simulating fan follow-up to test DM pull...")
    fan_followup = simulate_fan_reply(sean_reply)
    print(f"  [sim] @{handle}: {fan_followup}")
    used_pulls = state.get_dm_pulls(root_uri)
    dm_pull = generate_dm_pull_reply(
        original_text, fan_followup, handle, brand_voice, used_pulls=used_pulls
    )
    print(f"  [sim] → DM pull: {dm_pull}")


def _handle_engagement(notif, interaction_type, client, dm_state, state, dry_run):
    """
    Handle a like or repost notification by queuing a DM.
    Gated by: 1hr post age, active hours, bot scorer (fans only).
    Follows are handled separately via _handle_follow → queue_dm / process_dm_queue.
    """
    handle = notif.author.handle
    post_uri = getattr(notif, "reason_subject", None)

    if state.is_blocked(handle):
        print(f"  [skip] blocked: @{handle}")
        return

    # Classify user type (cached in dm_state.json)
    user_type, follower_count = _classify_user(handle, client, dm_state)
    print(f"  [user type: {user_type}, {follower_count:,} followers]")

    # Likes: only fans get outreach DMs
    if interaction_type == "like" and user_type != "fan":
        print(f"  [skip] {user_type} like — only DM fans for likes")
        return

    # Fetch profile for bot detection (fans) and DID
    try:
        profile = client.get_profile(handle)
    except Exception as e:
        print(f"  [warn] could not fetch profile for @{handle}: {e}")
        return

    # Bot scorer — applies to fan-type accounts only
    if user_type == "fan":
        score = bot_score(profile)
        if score >= BOT_SCORE_SKIP:
            print(f"  [skip] @{handle} bot score {score} ≥ {BOT_SCORE_SKIP}")
            return

    # Fetch post for context + 1hr age gate (same API call — created_at on record)
    post_context = ""
    post_created_at = None
    if post_uri:
        try:
            post = client.get_post(post_uri)
            post_context = post.record.text
            post_created_at = getattr(post.record, "created_at", None)
        except Exception:
            pass

    # 1hr post age gate — only queue DMs for engagements on recent posts
    if post_created_at:
        try:
            post_dt = datetime.fromisoformat(post_created_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - post_dt > timedelta(hours=1):
                print(f"  [skip] post >1hr old — not queueing DM")
                return
        except Exception:
            pass

    if dry_run:
        print(f"  [dry-run] would queue DM to @{handle} ({interaction_type})")
        return

    # Queue DM — send_engagement_dm() called later by execute-engagement-dms CF
    queue_dm(
        handle, profile.did, interaction_type, post_context, user_type,
        interaction_at=notif.indexed_at,
        post_created_at=post_created_at,
    )
    _write_engagement_event("engagement_queued", "inbound", handle, post_uri, user_type=user_type)


def _handle_follow(notif, client, dm_state, state, brand_voice, dry_run):
    """Handle a follow notification — highest priority outreach DM."""
    handle = notif.author.handle

    if state.is_blocked(handle):
        print(f"  [skip] blocked: @{handle}")
        return

    user_type, follower_count = _classify_user(handle, client, dm_state)
    print(f"  [follow] @{handle} ({user_type}, {follower_count:,} followers)")

    try:
        profile = client.get_profile(handle)
    except Exception as e:
        print(f"  [warn] could not fetch profile for @{handle}: {e}")
        return

    eligible, reason = _is_eligible_for_dm(profile)
    if not eligible:
        print(f"  [skip] @{handle} ineligible: {reason}")
        return

    queue_dm(handle, profile.did, "follow", "", user_type)
    _write_engagement_event("follow", "inbound", handle, None, user_type=user_type)


def run_once(client, state, brand_voice, dry_run, dm_state=None):
    metrics = {
        "notifications_seen": 0,
        "fan_nudge": 0,
        "fan_casual": 0,
        "fan_dm_pull": 0,
        "fan_discount_pull": 0,
        "creator_reply": 0,
        "studio_reply": 0,
        "themed_reply": 0,
        "subscriber_thanks": 0,
        "skipped_duplicate": 0,
        "skipped_blocked": 0,
        "skipped_paused": 0,
        "skipped_rate_limit": 0,
        "dms_queued": 0,
    }

    if not is_active_hours():
        print("[poll] outside active hours (7am–10pm Pacific) — skipping cycle")
        return metrics

    if state.get_status() == "paused":
        print("[paused] bot is paused, skipping cycle")
        metrics["skipped_paused"] = -1   # sentinel: whole cycle paused
        return metrics

    print("[poll] fetching notifications...")
    notifications = client.get_reply_notifications()
    print(f"[poll] {len(notifications)} reply notification(s)")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    for notif in notifications:
        if _is_seen(notif.uri):
            continue
        notif_time = datetime.fromisoformat(notif.indexed_at.replace("Z", "+00:00"))
        if notif_time < cutoff:
            handle = notif.author.handle
            try:
                has_convo = db.collection("conversations").document(handle).get().exists
            except Exception:
                has_convo = False
            if not has_convo:
                print(f"  [skip] too old ({notif.indexed_at[:10]}) — @{handle}")
                _mark_seen(notif.uri)
                continue
            print(f"  [keep] too old but active conversation — @{handle}")
        metrics["notifications_seen"] += 1
        print(f"\n[notif] {notif.uri} ({notif.indexed_at[:10]})")
        try:
            action = _handle_reply(notif, client, state, dm_state, brand_voice, dry_run)
            if action:
                metrics[action] = metrics.get(action, 0) + 1
        except Exception as e:
            print(f"  [error] {e}")
        _mark_seen(notif.uri)

    # Like / repost / follow → queue proactive DM
    dm_enabled = os.environ.get("DM_ENABLED", "true").lower() != "false"
    if dm_state is not None and dm_enabled:
        since = dm_state.get_last_checked_at()
        engagements = client.get_engagement_notifications(since=since)
        qualifier = f"since {since[:19]}Z" if since else "first run — no watermark yet"
        print(f"\n[poll] {len(engagements)} engagement notification(s) ({qualifier})")
        for notif in engagements:
            if _is_seen(notif.uri):
                continue
            print(f"\n[engagement] {notif.reason} by @{notif.author.handle}")
            try:
                if notif.reason == "follow":
                    _handle_follow(notif, client, dm_state, state, brand_voice, dry_run)
                else:
                    _handle_engagement(notif, notif.reason, client, dm_state, state, dry_run)
                metrics["dms_queued"] += 1
            except Exception as e:
                print(f"  [error] {e}")
            _mark_seen(notif.uri)
        dm_state.update_last_checked_at()

    return metrics


def main():
    parser = argparse.ArgumentParser(description="Bluesky reply bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate replies but do not post")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle then exit")
    parser.add_argument("--interval", type=int, default=60,
                        help="Poll interval in seconds (default: 60)")
    args = parser.parse_args()

    state = StateManager()
    dm_state = DMManager()
    brand_voice = load_brand_voice()
    client = BlueskyClient().login()

    print(f"[start] logged in as {os.environ['BLUESKY_HANDLE']}")
    if args.dry_run:
        print("[start] DRY RUN — replies will not be posted")

    if args.once:
        run_once(client, state, brand_voice, args.dry_run, dm_state=dm_state)
        poll_inbound_dms(client, brand_voice, dry_run=args.dry_run)
    else:
        while True:
            run_once(client, state, brand_voice, args.dry_run, dm_state=dm_state)
            poll_inbound_dms(client, brand_voice, dry_run=args.dry_run)
            print(f"\n[sleep] next poll in {args.interval}s...")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
