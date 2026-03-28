"""
Scan posts (optionally from N days ago), find the most-replied,
and simulate the full multi-round conversation flow for each reply.

Usage:
    python bluesky/reply/scan_and_test.py
    python bluesky/reply/scan_and_test.py --days-ago 10
    python bluesky/reply/scan_and_test.py --days-ago 10 --discount "20% off your first month — DM me"
    python bluesky/reply/scan_and_test.py --limit 20
"""
import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from bluesky.shared.bluesky_client import BlueskyClient
from bluesky.reply.reply_generator import (
    classify_post_type,
    classify_fan_intent,
    generate_reply,
    generate_dm_pull_reply,
    generate_peer_reply,
    simulate_fan_reply,
    load_brand_voice,
    GATED_POST_TYPES,
    PITCH_INTENT,
)
from bluesky.reply.state_manager import MAX_CONVERSATION_DEPTH
from bluesky.reply.creator_classifier import classify_replier, COLLAB_DM_THRESHOLD

_PAGE_SIZE = 50
_MAX_FETCH = 500  # hard ceiling to avoid runaway pagination


def _parse_dt(dt_str):
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def get_author_posts(client, handle, limit=None, until_days_ago=None):
    """
    Fetch original posts by handle.
    - limit: stop after this many posts (used when no date target)
    - until_days_ago: paginate until posts this old are found, then stop
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=until_days_ago)
        if until_days_ago else None
    )
    hard_limit = limit or _MAX_FETCH

    posts = []
    cursor = None

    while len(posts) < hard_limit:
        params = {"actor": handle, "limit": min(_PAGE_SIZE, hard_limit - len(posts))}
        if cursor:
            params["cursor"] = cursor

        response = client._client.app.bsky.feed.get_author_feed(params=params)
        page = [
            item.post for item in response.feed
            if item.post.author.handle == handle and not getattr(item, "reason", None)
        ]
        posts.extend(page)

        # If paginating by date: stop once oldest post on this page is old enough
        if cutoff and page:
            oldest = min(_parse_dt(p.indexed_at) for p in page)
            if oldest < cutoff:
                break

        cursor = getattr(response, "cursor", None)
        if not cursor or not page:
            break

    return posts


def filter_by_days_ago(posts, days_ago, window_days=2):
    target = datetime.now(timezone.utc) - timedelta(days=days_ago)
    window = timedelta(days=window_days)
    return [
        p for p in posts
        if abs((_parse_dt(p.indexed_at) - target).total_seconds()) < window.total_seconds()
    ]


def get_direct_replies(client, post_uri):
    response = client._client.app.bsky.feed.get_post_thread(params={"uri": post_uri})
    thread = response.thread
    replies = getattr(thread, "replies", None) or []
    return [r for r in replies if hasattr(r, "post")]


def simulate_conversation(original_text, reply_text, handle, brand_voice, post_type, discount, used_pulls):
    """
    Simulate a multi-round conversation from a single fan reply.
    Returns the updated used_pulls list.
    """
    is_gated = post_type in GATED_POST_TYPES
    current_context = original_text
    current_reply = reply_text
    round_num = 0

    while True:
        round_num += 1

        if is_gated:
            response = generate_reply(current_context, current_reply, handle, brand_voice, nudge=False)
            print(f"  [round {round_num} — gated, friendly only]")
            print(f"  → {response}")
            break

        intent = classify_fan_intent(current_reply)
        print(f"  [round {round_num} — intent: {intent}]")

        force_pitch = round_num > MAX_CONVERSATION_DEPTH

        if intent in PITCH_INTENT or force_pitch:
            use_discount = (
                discount
                and intent == "buying_signal"
                and post_type == "promotional"
            )
            dm_pull = generate_dm_pull_reply(
                current_context, current_reply, handle, brand_voice,
                used_pulls=used_pulls,
                discount=discount if use_discount else None,
            )
            tag = "dm pull"
            if use_discount:
                tag += ", discount"
            if force_pitch and intent not in PITCH_INTENT:
                tag += ", max depth"
            print(f"  [{tag}]")
            print(f"  → {dm_pull}")
            used_pulls.append(dm_pull)
            break
        else:
            nudge = generate_reply(current_context, current_reply, handle, brand_voice, nudge=True)
            print(f"  [nudge]")
            print(f"  → {nudge}")

            # Simulate fan's next reply
            fan_next = simulate_fan_reply(nudge)
            print(f"  [sim] @{handle}: {fan_next}")
            current_context = nudge
            current_reply = fan_next

    return used_pulls


def find_creator_replies(client, posts, brand_voice):
    """
    Scan up to 25 posts looking for replies from creators.
    Returns list of (post, reply_thread, creator_status) tuples.
    """
    found = []
    checked_posts = 0
    for post in posts:
        if checked_posts >= 25:
            break
        replies = get_direct_replies(client, post.uri)
        for rt in replies:
            handle = rt.post.author.handle
            try:
                profile = client.get_profile(handle)
                status = classify_replier(profile)
                if status.is_creator:
                    found.append((post, rt, status))
            except Exception:
                pass
        checked_posts += 1
    return found


_FAKE_SCENARIO_PROMPT = """Write a realistic Bluesky reply for this scenario:

Post: {post_text}

Creator: ~{follower_count} followers, peer in the same content niche.
Intent: {intent_description}

Reply with only the creator's message (1-2 sentences, casual Bluesky style). No labels, no quotes."""

_FAKE_SCENARIOS = [
    ("low_compliment",   2_500,  "leaving a genuine compliment or supportive comment, no ask"),
    ("low_collab",       2_500,  "expressing interest in collaborating or sliding into DMs"),
    ("high_no_collab",  42_000,  "leaving a genuine compliment or casual peer comment, no DM ask"),
    ("high_collab",     42_000,  "directly proposing a collab or asking to connect in DMs"),
]


def generate_fake_creator_replies(post_text):
    """Generate one reply per scenario for controlled peer register testing."""
    import anthropic
    ac = anthropic.Anthropic()
    results = []
    for scenario_id, follower_count, intent_desc in _FAKE_SCENARIOS:
        msg = ac.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            messages=[{"role": "user", "content": _FAKE_SCENARIO_PROMPT.format(
                post_text=post_text,
                follower_count=f"{follower_count:,}",
                intent_description=intent_desc,
            )}],
        )
        handle = scenario_id.replace("_", "")  # e.g. "lowcompliment"
        results.append((handle, follower_count, msg.content[0].text.strip(), scenario_id))
    return results


def run_peer_test(post_text, handle, follower_count, reply_text, brand_voice, label=""):
    tag = f"@{handle} ({follower_count:,} followers){' — ' + label if label else ''}"
    print(f"\n{'='*60}")
    print(f"  {tag}")
    print(f"  {reply_text[:100]}")
    print()
    options, peer_intent = generate_peer_reply(reply_text, handle, follower_count, brand_voice)
    print(f"  [peer intent: {peer_intent}]")
    if len(options) == 1:
        print(f"  → {options[0]}")
    else:
        print(f"  [decline options — pick one for the live bot]")
        for i, opt in enumerate(options, 1):
            print(f"  {i}. {opt}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Number of recent posts to fetch when not using --days-ago (default: 10)")
    parser.add_argument("--days-ago", type=int, default=None,
                        help="Target posts from approximately N days ago")
    parser.add_argument("--discount", type=str, default=None,
                        help="Discount offer to test as DM incentive")
    parser.add_argument("--creator", action="store_true",
                        help="Scan for creator replies and test peer register flow")
    args = parser.parse_args()

    brand_voice = load_brand_voice()
    client = BlueskyClient().login()

    # --- Creator peer register test ---
    if args.creator:
        limit = args.limit or 25
        print(f"[creator scan] checking replies across last {limit} posts for @{client.handle}...")
        all_posts = get_author_posts(client, client.handle, limit=limit)
        creator_hits = find_creator_replies(client, all_posts, brand_voice)

        if creator_hits:
            print(f"[creator scan] found {len(creator_hits)} creator reply/replies\n")
            for post, rt, status in creator_hits:
                tier = "high follower" if status.follower_count >= COLLAB_DM_THRESHOLD else "low follower"
                run_peer_test(
                    post.record.text,
                    rt.post.author.handle,
                    status.follower_count,
                    rt.post.record.text,
                    brand_voice,
                    label=f"{tier}, detected via {status.signal}",
                )
        else:
            print("[creator scan] no creator replies found — generating fake exchange\n")
            base = next((p for p in all_posts if (p.reply_count or 0) > 0), all_posts[0])
            print(f"[fake] using post: {base.record.text[:80]}\n")
            fakes = generate_fake_creator_replies(base.record.text)
            for handle, follower_count, reply_text, scenario_id in fakes:
                label = scenario_id.replace("_", " ")
                run_peer_test(base.record.text, handle, follower_count, reply_text, brand_voice, label=label)
        return

    if args.days_ago:
        print(f"[scan] paginating back ~{args.days_ago} days for @{client.handle}...")
        all_posts = get_author_posts(client, client.handle, until_days_ago=args.days_ago + 2)
        if all_posts:
            oldest = min(_parse_dt(p.indexed_at) for p in all_posts)
            newest = max(_parse_dt(p.indexed_at) for p in all_posts)
            print(f"[scan] fetched {len(all_posts)} posts spanning {oldest.strftime('%b %d')} – {newest.strftime('%b %d')}")
        posts = filter_by_days_ago(all_posts, args.days_ago)
        if not posts:
            print(f"[scan] no posts found in the ±2 day window around {args.days_ago} days ago")
            if all_posts:
                print(f"[scan] oldest post fetched: {oldest.strftime('%Y-%m-%d')} — try a smaller --days-ago value")
            return
        print(f"[scan] {len(posts)} post(s) matched")
    else:
        limit = args.limit or 10
        print(f"[scan] fetching last {limit} posts for @{client.handle}...")
        posts = get_author_posts(client, client.handle, limit=limit)
        if not posts:
            print("[scan] no posts found")
            return
        print(f"[scan] {len(posts)} post(s) found")

    for p in posts:
        print(f"  ({p.reply_count or 0} replies) {p.record.text[:80]}")

    most_replied = max(posts, key=lambda p: p.reply_count or 0)
    post_type = classify_post_type(most_replied.record.text)
    is_gated = post_type in GATED_POST_TYPES

    print(f"\n[target] {most_replied.reply_count} replies — post type: {post_type}{' (gated)' if is_gated else ''}")
    print(f"  {most_replied.record.text[:120]}")
    print(f"  {most_replied.uri}\n")

    if args.discount:
        print(f"[discount] testing incentive: {args.discount}\n")

    replies = get_direct_replies(client, most_replied.uri)
    print(f"[scan] {len(replies)} direct reply/replies\n")

    if not replies:
        print("[scan] nothing to test")
        return

    original_text = most_replied.record.text
    used_pulls = []  # shared across all replies in this run for phrase variety

    for i, reply_thread in enumerate(replies, 1):
        post = reply_thread.post
        handle = post.author.handle
        reply_text = post.record.text

        print(f"{'='*60}")
        print(f"  Reply {i}/{len(replies)} — @{handle}")
        print(f"  {reply_text[:100]}\n")

        used_pulls = simulate_conversation(
            original_text, reply_text, handle, brand_voice,
            post_type, args.discount, used_pulls,
        )
        print()


if __name__ == "__main__":
    main()
