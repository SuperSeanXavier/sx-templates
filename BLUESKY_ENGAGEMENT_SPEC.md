# Bluesky Fan Engagement System — Build Spec
> **For Claude Code** · Python · Read the existing codebase before starting anything.

---

## What This Is

You are building a Bluesky fan engagement system for a single creator. The system runs continuously in the background, monitors the creator's Bluesky account for engagement events, and responds in the creator's brand voice — through comments on target community posts, replies to fans, and DMs to engaged followers.

This is an extension of an existing Python codebase. Do not rebuild what already exists. Read the codebase first and understand what you have before writing a single line.

---

## Existing Dependencies — Do Not Rebuild

Before you start, locate and read these in the codebase:

- **Brand voice / AI generation pipeline** — the existing module that generates text in a creator's tone. All text generation in this system routes through it. Do not write a parallel implementation.
- **Bluesky auth / API connection** — existing authentication using app passwords. `BlueskyClient.login()` is called once at process startup; the atproto SDK holds the session in memory and handles token refresh transparently. All Bluesky API calls use this client. Never call `login()` more than once per process start.
- **Database layer** — Firestore is the database. All persistent state, queues, event logs, and conversation history live in Firestore collections. Do not use JSON files for persistent state in new components. The existing `state.json` and `dm_state.json` files are legacy — new components write to Firestore only.

---

## What You Are Building

Four loosely coupled components. Build and test them in this order.

---

### Component 1: Audience Discovery

**Goal:** Produce a ranked list of target accounts in three domains that the system will monitor for comment opportunities.

**The three domains:**
- Sex positivity and education
- Relationship and dating advice
- Gay adult content

**Two discovery techniques feed into a single ranked output:**

**Starter pack discovery** — Search Bluesky starter packs by domain-relevant keywords. Pull member lists from qualifying packs. Score and rank members as potential anchor accounts. An anchor account is someone whose posts are worth commenting on because their audience overlaps with the creator's target audience.

**Follower graph analysis** — Pull the creator's followers (cap at 2,000 for API efficiency). For each follower, pull who they follow (cap at 500 per follower). Count how frequently each external account appears across the follower base. Accounts that appear frequently are ones the creator's existing audience already follows — high-value targets.

**Intersection scoring** — Combine both outputs. An account appearing in both lists is a Tier 1 target. Appearing only in the follower graph is Tier 2. Appearing only in starter packs is Tier 3. Store all three tiers in the database with their scores.

**Acceptance criteria:**
- Running discovery for a domain produces a persisted, ranked list of target accounts
- Accounts are deduplicated across keyword searches and pack memberships
- Tier classification is correct and queryable
- Discovery respects the read rate limit (see Rate Limits section)
- Starter pack discovery runs on a weekly schedule
- Follower graph analysis runs on a daily schedule, overnight

---

### Component 2: Comment Engine

**Goal:** Monitor target account posts, identify good comment opportunities, generate on-brand comments, and post them at a human-paced cadence.

**Post discovery** — Every 15 minutes, pull recent posts from Tier 1 and Tier 2 target accounts. Score each post for comment opportunity. A good comment opportunity is a post that is recent, has engagement between 15 and 150 interactions (likes + reposts combined — below 15 is too cold, above 150 risks getting lost in noise), is a question or opinion post rather than pure announcement, and has not already been commented on by the creator.

**Comment generation** — Pass qualifying posts to the existing brand voice pipeline with appropriate context: the post content, the domain it belongs to, and the instruction that the comment must add something specific — not generic reaction, not promotional, not a question that could apply to anything. The generator should be prompted to produce a comment that makes the original poster want to reply.

**Posting** — Comments go into a queue. One comment is dequeued and posted every 20–30 minutes (randomized). Hard daily cap of 50 comments regardless of queue depth. Never comment on the same post twice. Never post more than one comment within a 4-minute window under any circumstances.

**Acceptance criteria:**
- Post discovery runs every 15 minutes and populates a comment queue
- Queue executor posts one comment per 20–30 minute randomized window
- Daily cap of 50 enforced — queue carries forward to next day when cap is hit
- No duplicate comments on the same post
- All generated comments pass through the existing brand voice pipeline
- Comment and its target post are logged to the database

---

### Component 3: Fan Engagement Pipeline

**Goal:** Respond to fan engagement events (likes, reposts, follows, replies) with appropriate outbound actions — replies into threads and DMs to fans.

**Notification polling** — Poll `app.bsky.notification.listNotifications` every 5 minutes. For each new notification, classify the event type (reply, like, repost, follow) and store it. Use a seen-events table to deduplicate — never process the same notification URI twice.

**Reply handling** — When a fan replies to the creator's post, fetch the original post content and the fan's reply text. Pass both to the brand voice pipeline to generate a contextual reply. Post it back into the thread with a randomized delay of 90–600 seconds. Log it.

**DM outreach queue** — Every 2 hours, process accumulated like, repost, and follow events. For each event, apply the eligibility filters below. Qualifying fans are added to the DM outreach queue with a priority level (follow = highest, repost = high, like = standard).

**DM eligibility filters — enforce all of these:**
- Fan must have more than 50 followers
- Account must not appear to be a bot (zero posts, follower count under 10, or following-to-follower ratio above 20x)
- Daily DM cap must not already be reached (hard cap: 50 per day)

**DM batch executor** — Every 4 hours, dequeue up to 7 DMs and send them. Minimum 8 minutes between sends within a batch, maximum 20 minutes (randomized). Each DM is generated by the brand voice pipeline with the trigger context: what the fan did, what the original post was about, and the instruction that this is a first message — warm, personal, no links, no promotion. (Batch size is 7, not 15, to fit within the 3600s Cloud Function timeout at 8-min minimum stagger.)

**Inbound DM handling** — Poll active DM conversations for new messages every 5 minutes. When a fan replies to an outbound DM, fetch the full conversation history and pass it to the brand voice pipeline to generate a reply. Maintain conversation stage in the database (warm → engaged → converted / handed off).

**Existing reply/DM routing survives** — The existing fan routing logic (nudging questions, DM pull replies, discount offers, subscriber guard, creator/themed/studio routing) is preserved in the reply handler. The new engagement pipeline adds proactive outreach on top of it; it does not replace the existing reply behavior.

**Acceptance criteria:**
- All five event types (reply, like, repost, follow, inbound DM) are handled
- All eligibility filters enforced before any DM is queued
- Daily DM cap of 50 enforced hard — no exceptions
- Conversation history is maintained and passed on every AI generation call
- First outbound DM never contains links, pricing, or promotional content
- All outbound actions logged with timestamp, trigger event, and generated text

---

### Component 4: Human Handoff

**Goal:** Detect conversations that the AI should not continue and alert a human immediately.

**Trigger any of these conditions and stop all automated responses in that thread:**
- Fan directly asks if they are speaking to a real person
- Fan mentions pricing, rates, or custom content requests
- Fan expresses distress or sends abusive content
- Conversation has exceeded 8 exchanges with no resolution
- The AI generation pipeline returns a low-confidence or flagged output

**When a handoff is triggered:**
- Set a `human_handoff` flag on the conversation record
- Log the reason
- Do not send any further automated messages in that thread
- Expose the flagged conversation clearly in whatever logging or output mechanism the codebase already uses

**The flag must be manually cleared before automated responses resume in that thread.**

**Acceptance criteria:**
- All five handoff triggers are detected reliably
- Flagged conversations receive no further automated messages
- Handoff reason is logged
- Flag requires manual clearance to resume

---

## Rate Limits — Respect These

Bluesky enforces the following. Build around them, do not fight them.

| Limit | Value | Notes |
|---|---|---|
| Write operations | 5,000 points/hour, 35,000/day | CREATE = 3pts, UPDATE = 2pts, DELETE = 1pt. Max ~1,666 creates/hour |
| API read requests | 3,000 per 5 minutes | **IP-based** — shared across all accounts running from the same server |
| Session creation | 300 per day per account | `login()` is called once at process startup. Token lives in memory; atproto SDK refreshes it transparently. Do not re-authenticate on each request or each cycle. |

**Build a centralized rate limit manager** that all components route API calls through. No component should call the Bluesky API directly without going through this manager. The manager should track read budget, write budget, and enforce backoff when limits are approached — not when they are hit.

Leave headroom. Target 80% of limits as the operational ceiling, not 100%.

---

## Scheduling Summary

Scheduling is handled by **GCP Cloud Scheduler**, not in-process. Each task is a separate Cloud Function or Cloud Run job triggered by Cloud Scheduler on the intervals below. Tasks do not block each other.

| Frequency | Task |
|---|---|
| Every 5 minutes | Poll notifications · Poll active DM conversations |
| Every 15 minutes | Scan target account posts for comment opportunities |
| Every 20–30 min (randomized) | Post one queued comment |
| Every 2 hours | Process engagement events into DM queue |
| Every 4 hours | Execute DM batch (max 7 per run, staggered sends) |
| Weekly | Cleanup stale docs (seen_events, dm_queue, comment_queue) |
| Daily (overnight) | Follower graph analysis · Rescore target accounts |
| Weekly | Starter pack discovery · Rebuild target account lists |

---

## Database — Firestore Collections

All persistent state lives in Firestore. Use Firestore document conventions — collections of documents, not relational tables. Add the following collections incrementally as each component is built.

**Collections to add:**

`target_accounts` — one document per anchor account per domain. Fields: handle, did, tier (1/2/3), score, discovery_sources (array), domain, last_scored_at.

`engagement_events` — raw log of incoming notification events. Fields: event_type, fan_did, fan_handle, post_uri, indexed_at, processed (bool), processed_at.

`dm_queue` — outbound DMs awaiting sending. Fields: fan_did, fan_handle, trigger_type, post_context, priority, status (pending/sent/skipped), created_at, sent_at.

`conversations` — one document per active DM thread, keyed by fan handle. Fields: convo_id, stage (warm/engaged/converted/handed_off), human_handoff (bool), handoff_reason, trigger_context, created_at, last_message_at.

`messages` — subcollection under each `conversations` document. Fields: role (user/assistant/human), content, timestamp.

`comment_queue` — generated comments awaiting posting. Fields: target_post_uri, target_account, domain, generated_text, score, status (pending/posted/skipped), created_at, posted_at.

`seen_events` — deduplication log for notification URIs. Fields: uri, seen_at. Used to prevent double-processing. Cleaned up weekly (entries older than 7 days are deleted by `cleanup-stale-docs`).

---

## Behavioral Constraints — Non-Negotiable

These are not implementation suggestions. Every one of these must hold true in production.

- Never re-authenticate on every request. Use the existing token management.
- Never post the same comment on the same post twice.
- Never DM the same account more than once through the outreach system.
- Never send a first DM that contains a link, a platform name, pricing, or a call to action.
- Never post more than one write action within a 4-minute window without a randomized delay. This is a **global window** — a comment post and a DM send cannot both occur within the same 4-minute window. All write actions coordinate through a shared last-write timestamp in Firestore.
- Never process the same notification URI twice.
- Never resume automated messages in a conversation with an active handoff flag.
- Daily caps (50 DMs, 50 comments) are hard limits. Queue carries forward — do not drop items.

---

## What Good Looks Like

The system is working correctly when:

- A fan likes a post, receives a warm personal DM within a few hours, replies, and has a natural multi-turn conversation — all in the creator's voice
- A comment posted on a Tier 1 target account's post reads like a genuine, specific reaction from a person who cares about the topic
- The daily DM cap is never exceeded even on high-traffic days
- No fan is contacted twice through the outreach system
- A conversation that triggers a handoff goes quiet immediately and stays quiet until manually cleared
- The system runs overnight without hitting a rate limit or authentication error

---

## What To Do First

1. Read the entire existing codebase before writing anything
2. Identify the exact entry points for the brand voice pipeline and Bluesky auth
3. Set up the Firestore connection and confirm the GCP project (`sx-platform`) is accessible
4. Build the rate limit manager first — everything else depends on it
5. Build and test Component 1 (discovery) in isolation before moving to the others
6. Add Firestore collections incrementally as each component needs them — do not create all collections upfront
7. Build Component 3 (fan engagement) before Component 2 (comments) — inbound engagement is higher priority than outbound discovery
8. Each component is a separate Cloud Function. Build and deploy them one at a time.