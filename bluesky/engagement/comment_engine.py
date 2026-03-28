"""
Comment engine — monitor Tier 1/2 target posts and comment at human pace.

scan_target_posts(client)
    Pull recent posts from Tier 1 + Tier 2 accounts in `target_accounts`.
    Score each post by engagement (15–150 threshold) and novelty.
    Qualifying posts → write to Firestore `comment_queue` (status=pending).
    Run every 15 minutes via Cloud Scheduler.

execute_comment_queue(client, brand_voice)
    Dequeue the highest-scoring pending comment (already generated or generate now).
    Enforce global 4-min write window and daily cap of 50.
    Run every 20 min via Cloud Scheduler with a short in-process random sleep
    (0–10 min) to avoid a predictable posting cadence.
"""
import os
import random
import time
from datetime import datetime, timezone, date

import anthropic

from google.cloud.firestore_v1.base_query import FieldFilter as _filter
from bluesky.shared.firestore_client import db
from bluesky.shared.rate_limiter import (
    check_read, check_write, seconds_until_next_write, RateLimitError,
)

_QUEUE = db.collection("comment_queue")

# Engagement window: only comment on posts in this likes+reposts range
ENGAGEMENT_MIN = 15
ENGAGEMENT_MAX = 150

DAILY_COMMENT_CAP = int(os.environ.get("DAILY_COMMENT_CAP", "50"))

# ---------------------------------------------------------------------------
# Comment generation
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You write Bluesky comments on behalf of the creator described in the brand "
    "voice below. Write ONLY the comment text — no quotes, no labels, nothing else."
)

_COMMENT_PROMPT = """{brand_voice}

---

You're about to comment on a post by @{target_handle} on Bluesky.

Their post:
{post_text}

---

Write a short comment that:
- Adds something specific to what they said — a reaction, a question, a personal take
- Feels natural and human, not promotional
- Makes them want to reply (curious, affirming, or playfully provocative)
- Is NOT generic ("love this!", "great post!", "so true")
- Does NOT mention subscribing, OnlyFans, or any platform
- Does NOT include any links
{domain_line}
Do not add emojis unless they feel completely natural.
1–2 sentences maximum."""


def _generate_comment(target_handle, post_text, brand_voice, domains=None):
    """Generate a comment for a target post using the brand voice."""
    domain_line = ""
    if domains:
        domain_line = f"Domain context (what this account is about): {', '.join(domains)}\n"

    prompt = _COMMENT_PROMPT.format(
        brand_voice=brand_voice,
        target_handle=target_handle,
        post_text=post_text[:400],
        domain_line=domain_line,
    )

    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=120,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ---------------------------------------------------------------------------
# 8a — Post scoring and queue population
# ---------------------------------------------------------------------------

def _engagement(post_view):
    """likes + reposts on a FeedViewPost."""
    record = getattr(post_view, "post", post_view)
    likes = getattr(record, "like_count", 0) or 0
    reposts = getattr(record, "repost_count", 0) or 0
    return likes + reposts


def _post_text(post_view):
    """Extract plain text from a FeedViewPost."""
    post = getattr(post_view, "post", post_view)
    record = getattr(post, "record", None)
    return (getattr(record, "text", "") or "").strip()


def _already_queued(post_uri):
    """True if this post URI is already in the comment_queue."""
    docs = (
        _QUEUE
        .where(filter=_filter("post_uri", "==", post_uri))
        .limit(1)
        .stream()
    )
    return any(True for _ in docs)


def _already_commented(post_uri):
    """True if we've already posted a comment on this URI (checked via comment_queue)."""
    docs = (
        _QUEUE
        .where(filter=_filter("post_uri", "==", post_uri))
        .where(filter=_filter("status", "==", "posted"))
        .limit(1)
        .stream()
    )
    return any(True for _ in docs)


def scan_target_posts(client):
    """
    Fetch recent posts from Tier 1 and Tier 2 accounts.
    Score by engagement; add qualifying posts to comment_queue.
    """
    print("[comment] scanning target posts...")

    my_did = client._my_did

    # Load Tier 1 + 2 accounts
    docs = (
        db.collection("target_accounts")
        .where(filter=_filter("tier", "<=", 2))
        .stream()
    )
    targets = [{"did": d.id, **d.to_dict()} for d in docs]
    print(f"[comment] {len(targets)} Tier 1/2 account(s) to scan")

    queued = 0
    skipped_engagement = 0
    skipped_duplicate = 0
    skipped_meaningless = 0
    posts_evaluated = 0

    for target in targets:
        handle = target.get("handle")
        domains = target.get("domains", [])
        if not handle:
            continue

        try:
            check_read()
            resp = client.get_author_feed(handle, limit=10)
        except RateLimitError as e:
            print(f"  [rate] {e} — pausing 60s")
            time.sleep(60)
            continue
        except Exception as e:
            print(f"  [warn] feed fetch failed for @{handle}: {e}")
            continue

        feed = getattr(resp, "feed", []) or []

        for item in feed:
            post = getattr(item, "post", None)
            if not post:
                continue

            post_uri = getattr(post, "uri", None)
            if not post_uri:
                continue

            # Skip own posts
            author_did = getattr(getattr(post, "author", None), "did", None)
            if author_did == my_did:
                continue

            posts_evaluated += 1

            # Engagement gate
            eng = _engagement(item)
            if eng < ENGAGEMENT_MIN or eng > ENGAGEMENT_MAX:
                skipped_engagement += 1
                continue

            # Skip if already queued or posted
            if _already_queued(post_uri) or _already_commented(post_uri):
                skipped_duplicate += 1
                continue

            text = _post_text(item)
            if not text:
                continue

            # Skip pure announcements (very short posts with links/handles only)
            meaningful_words = [w for w in text.split() if not w.startswith(("http", "@", "#"))]
            if len(meaningful_words) < 4:
                skipped_meaningless += 1
                continue

            post_cid = getattr(post, "cid", "")
            root = getattr(getattr(item, "reply", None), "root", None)
            root_uri = getattr(root, "uri", post_uri) if root else post_uri
            root_cid = getattr(root, "cid", post_cid) if root else post_cid

            _QUEUE.add({
                "post_uri": post_uri,
                "post_cid": post_cid,
                "root_uri": root_uri,
                "root_cid": root_cid,
                "target_handle": handle,
                "target_did": author_did,
                "post_text": text[:400],
                "engagement": eng,
                "domains": domains,
                "status": "pending",
                "comment_text": None,
                "queued_at": datetime.now(timezone.utc).isoformat(),
                "posted_at": None,
            })
            queued += 1
            print(f"  [queued] @{handle} (eng:{eng}) — {text[:60]}")

    print(f"[comment] scan complete — {queued} post(s) queued")
    return {
        "targets_scanned": len(targets),
        "posts_evaluated": posts_evaluated,
        "posts_queued": queued,
        "skipped_engagement": skipped_engagement,
        "skipped_duplicate": skipped_duplicate,
        "skipped_meaningless": skipped_meaningless,
    }


# ---------------------------------------------------------------------------
# 8b/8c — Generate + execute
# ---------------------------------------------------------------------------

def _daily_posted_count():
    today = date.today().isoformat()
    docs = (
        _QUEUE
        .where(filter=_filter("status", "==", "posted"))
        .where(filter=_filter("posted_at", ">=", today))
        .stream()
    )
    return sum(1 for _ in docs)


def _next_pending():
    """Return the highest-engagement pending item, or None."""
    docs = (
        _QUEUE
        .where(filter=_filter("status", "==", "pending"))
        .stream()
    )
    items = [{"id": d.id, **d.to_dict()} for d in docs]
    if not items:
        return None
    return max(items, key=lambda x: x.get("engagement", 0))


def execute_comment_queue(client, brand_voice, dry_run=False):
    """
    Dequeue one pending comment, generate text if not already generated, post it.
    Random 0–10 min sleep at entry provides cadence variation when called on a
    fixed Cloud Scheduler trigger.
    """
    # Randomized entry delay — avoids clockwork posting pattern
    if not dry_run:
        jitter = random.uniform(0, 600)
        print(f"[comment] jitter sleep {jitter / 60:.1f} min...")
        time.sleep(jitter)

    # Daily cap
    posted_today = _daily_posted_count()
    if posted_today >= DAILY_COMMENT_CAP:
        print(f"[comment] daily cap reached ({posted_today}/{DAILY_COMMENT_CAP})")
        return {
            "outcome": "cap_hit",
            "cap_used": posted_today,
            "cap_remaining": 0,
        }

    item = _next_pending()
    if not item:
        print("[comment] no pending comments")
        return {
            "outcome": "empty_queue",
            "cap_used": posted_today,
            "cap_remaining": DAILY_COMMENT_CAP - posted_today,
        }

    doc_ref = _QUEUE.document(item["id"])
    handle = item["target_handle"]
    post_text = item["post_text"]
    domains = item.get("domains", [])

    # Generate comment text if not already done
    comment_text = item.get("comment_text")
    if not comment_text:
        try:
            comment_text = _generate_comment(handle, post_text, brand_voice, domains)
            doc_ref.update({"comment_text": comment_text})
        except Exception as e:
            print(f"  [warn] generation failed for @{handle}: {e}")
            doc_ref.update({"status": "skipped", "skip_reason": "generation_failed"})
            return {
                "outcome": "generation_failed",
                "target_handle": handle,
                "cap_used": posted_today,
                "cap_remaining": DAILY_COMMENT_CAP - posted_today,
            }

    print(f"  [comment → @{handle}] {comment_text[:120]}")

    if dry_run:
        print("  [dry-run]")
        return {
            "outcome": "dry_run",
            "target_handle": handle,
            "domains": domains,
            "cap_used": posted_today,
            "cap_remaining": DAILY_COMMENT_CAP - posted_today,
        }

    # Enforce global 4-min write window
    wait = seconds_until_next_write()
    if wait > 0:
        print(f"  [rate] waiting {wait:.0f}s for write window...")
        time.sleep(wait)

    try:
        check_write("create")
        client.post_reply(
            text=comment_text,
            parent_uri=item["post_uri"],
            parent_cid=item["post_cid"],
            root_uri=item["root_uri"],
            root_cid=item["root_cid"],
        )
        now = datetime.now(timezone.utc).isoformat()
        doc_ref.update({
            "status": "posted",
            "posted_at": now,
        })
        print(f"  [posted] ({posted_today + 1}/{DAILY_COMMENT_CAP} today)")
        return {
            "outcome": "posted",
            "target_handle": handle,
            "domains": domains,
            "cap_used": posted_today + 1,
            "cap_remaining": DAILY_COMMENT_CAP - (posted_today + 1),
        }

    except RateLimitError as e:
        print(f"  [rate limit] {e}")
        return {
            "outcome": "window_blocked",
            "target_handle": handle,
            "cap_used": posted_today,
            "cap_remaining": DAILY_COMMENT_CAP - posted_today,
        }
    except Exception as e:
        print(f"  [error] post failed for @{handle}: {e}")
        doc_ref.update({"status": "skipped", "skip_reason": f"post_failed: {e}"})
        return {
            "outcome": "post_failed",
            "target_handle": handle,
            "cap_used": posted_today,
            "cap_remaining": DAILY_COMMENT_CAP - posted_today,
        }
