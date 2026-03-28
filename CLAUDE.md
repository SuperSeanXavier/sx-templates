# CLAUDE.md — sx-templates

## Project Purpose

Sean Xavier's creator platform — automation tools for Bluesky and other social channels. Also the reference implementation for the Peachy Creator Kit: Sean's version is built and proven here first, then patterns are generalized for the kit. Troubleshoot here; export to the kit when stable.

---

## BrandVoice

Brand voice is defined by a versioned template schema:

```
brandvoice/brandvoice-template-v1.md   ← schema (structure only, no creator values)
```

Sean's filled instance lives in a separate private repo:

```
/Users/ko/Documents/TestContentGenerator/.claude/SX_Instructions.md
```

The Bluesky bot reads it via the `BRANDVOICE_PATH` env variable.

**Do NOT duplicate brand voice content here** — always read from the source file.

**When the template is updated to v2:**
1. Diff `brandvoice-template-v1.md` → `brandvoice-template-v2.md`
2. Update `SX_Instructions.md` in TestContentGenerator to conform
3. Bump `template_version` in `SX_Instructions.md` frontmatter

---

## Bluesky Reply Bot

Entry point: `bluesky/reply/poller.py`

```bash
# Test — generates replies, prints them, does NOT post
python bluesky/reply/poller.py --dry-run --once

# One real cycle
python bluesky/reply/poller.py --once

# Run continuously (default 60s interval)
python bluesky/reply/poller.py --interval 60

# Admin controls
python bluesky/reply/admin.py status
python bluesky/reply/admin.py pause-all
python bluesky/reply/admin.py resume
python bluesky/reply/admin.py pause-user @handle
python bluesky/reply/admin.py block-user @handle
python bluesky/reply/admin.py unblock-user @handle
```

### scan_and_test.py — development tool

Used to test reply logic against real or simulated data without running the full poller.

```bash
# Test fan reply flow on most-replied post (last 10)
python bluesky/reply/scan_and_test.py

# Target posts from ~N days ago (paginates automatically)
python bluesky/reply/scan_and_test.py --days-ago 3

# Test peer/creator register flow (scans last 25 posts for creator replies)
# Falls back to a generated fake exchange if none found
python bluesky/reply/scan_and_test.py --creator
```

---

## Reply Logic

### Fan reply routing

All post types (personal, casual, promotional, content) feed the same funnel. A 75% gate controls nudging — 25% of replies are friendly conversation only, regardless of post type.

```
75% of fan replies:
    first reply   → nudging question (steers toward DMs)
    follow-up     → classify_fan_intent()
        buying_signal or curious  → DM pull (no discount code in public reply)
        casual / no signal        → another nudging question
        depth >= MAX_DEPTH (3)    → force DM pull
    after DM pull → Firestore conversations doc created for the fan

25% of fan replies:
    → friendly reply only, no funnel
```

### Subscriber guard
Before any fan routing: if the reply mentions being an existing subscriber/member, send a warm thank-you and exit — no nudging question, no DM pull, no discount.

### Discount rules

Discounts are offered **only inside DM conversations** — never in public reply DM pulls.

**DM conversation discount** (`poll_inbound_dms`):
- Fan's DM is classified as `buying_signal` or `curious`
- `FAN_DISCOUNT_CODE` env var is set (falls back to legacy `DISCOUNT_OFFER`)
- `discount_sent` flag is not already set on the conversation Firestore doc (one discount per fan ever)
- Link used: `FAN_DISCOUNT_URL_DM` (falls back to `FAN_DISCOUNT_URL_REPLY`)

After the discount is sent, `discount_sent: true` is written to the conversation doc. Subsequent messages from the same fan route to `generate_dm_funnel_reply` with `discount=None`, which steers toward the site without repeating the code.

Tracking URLs by source:
| Source | URL env var |
|---|---|
| DM conversation (any trigger) | `FAN_DISCOUNT_URL_DM` → fallback `FAN_DISCOUNT_URL_REPLY` |
| Like-triggered DM | `FAN_DISCOUNT_URL_LIKE` *(initial outreach only — no discount)* |
| Repost-triggered DM | `FAN_DISCOUNT_URL_REPOST` *(initial outreach only — no discount)* |

### DM pull phrase variety
Used pulls are tracked per root post URI in `state.json`. Each new DM pull is shown previous ones and told to avoid similar phrasing.

### Creator / peer register

Creator detection is flag-based. For Sean's account: `CREATOR_DETECTION_MUTUAL_FOLLOW=true` only (Sean only follows creators). The template supports all three signals.

| Signal | Flag | Notes |
|---|---|---|
| Mutual follow | `CREATOR_DETECTION_MUTUAL_FOLLOW` | Sean follows them — primary signal for personal accounts |
| Bio keywords | `CREATOR_DETECTION_BIO` | OnlyFans, Fansly, "creator", "model", 18+, etc. |
| Follower count | `CREATOR_DETECTION_FOLLOWER_COUNT` | Above `CREATOR_FOLLOWER_THRESHOLD` (default 500) |

When two or more signals are active, two must trigger (except mutual_follow alone is sufficient).

**Peer reply routing:**

| Scenario | Response |
|---|---|
| ≥ 20k followers, DM-seeking | Collab DM pull |
| ≥ 20k followers, compliment | Warm peer acknowledgment |
| ≥ 20k followers, general | Peer conversation, DMs mentioned if natural |
| < 20k followers, compliment | Brief thank / emoji |
| < 20k followers, DM-seeking | 3 polite decline options (bot picks randomly in live mode) |
| < 20k followers, general | Warm and brief, no DM |

`COLLAB_DM_THRESHOLD` (default 20000) controls the high/low follower split.

---

## State

**Persistence strategy:** New components (engagement system, comment engine, audience discovery) write to **Firestore** (`GOOGLE_CLOUD_PROJECT=sx-platform`). The existing reply bot still uses JSON files. When the reply bot is migrated to Firestore, only `state_manager.py` and `dm_manager.py` need to change.

### Legacy JSON files (existing reply bot only)

**`bluesky/reply/state.json`** — reply bot state (`state_manager.py`).
- `replied_posts` — URIs already handled (dedup)
- `my_reply_uris` — URIs of the bot's own replies (follow-up detection)
- `dm_pulls_by_root` — DM pull text per root post URI (phrase variety)
- `conversation_depth` — follow-up round count per root post URI
- `daily_discounts` — discount count per date (legacy field; no longer incremented — per-handle cap is now `discount_sent` in Firestore)
- `blocked_users` — permanent skip list
- `paused_users` — temporary per-user pause
- `bot_status` — `"running"` | `"paused"`

**`bluesky/reply/dm_state.json`** — proactive DM state (`dm_manager.py`).
- `conversations` — per-handle classification cache: `user_type`, `follower_count`, `classified_at`
- `last_checked_at` — watermark timestamp for engagement notification polling

Note: conversation anti-spam state (`consecutive_mine`, `last_sender`, `convo_id`) and interaction dedup (`processed_interactions`) have been moved to Firestore and are no longer in this file.

### Firestore collections (new engagement system)

See `BLUESKY_ENGAGEMENT_SPEC.md` → Database section for full field specs.

| Collection | Purpose |
|---|---|
| `target_accounts` | Ranked anchor accounts per domain (tiers 1–3) |
| `engagement_events` | Raw notification log — reply, like, repost, follow |
| `dm_queue` | Outbound DMs awaiting batch send |
| `conversations` | Per-fan DM thread state and stage (`warm` → `engaged` / `converted` / `subscriber`; `dm_pull_sent` for public-reply-to-DM path) |
| `messages` | Full message history per conversation (subcollection) |
| `comment_queue` | Generated comments awaiting posting |
| `seen_events` | Notification URI dedup log |

---

## User Types

Four types, classified in priority order. Classification is cached in `dm_state.json` for `CLASSIFICATION_TTL_DAYS` (default 30) days to avoid repeat API calls.

| Type | Detection |
|---|---|
| `studio` | Manual list (`STUDIO_HANDLES`) or bio keywords (productions, studio, films, etc.) |
| `themed` | Manual list (`THEMED_HANDLES`) or bio keywords (muscle worship, big dick appreciation, etc.) |
| `creator` | Existing three-signal system (mutual_follow, bio, follower_count) |
| `fan` | Everyone else |

### Reply routing by type
| Type | Public reply behavior |
|---|---|
| Studio | Simple warm thanks — no pitch, no collab |
| Themed | Playful, niche-aware — lean into fitting their aesthetic |
| Creator | Existing peer register routing |
| Fan | Existing fan routing (subscriber guard → nudge → DM pull) |

### Proactive DM routing (likes/reposts)

Triggered by likes and reposts. Controlled by `DM_ENABLED` env var (default `true`).

**Likes and reposts are sent immediately** via `send_engagement_dm()` in `poll-notifications` — no queue, no batch delay. The fan is active right now; immediacy increases engagement likelihood.

**Follows** are still queued via `queue_dm` / `execute-dm-batch` — no urgency.

| Type | Like | Repost |
|---|---|---|
| Fan | Thank-you DM (immediate) | Thank-you DM, higher energy (immediate) |
| Creator | Skip | Peer-register thank-you DM (immediate) |
| Themed | Skip | Playful, niche-aware thank-you DM (immediate) |
| Studio | Skip | Simple professional thank-you DM (immediate) |

DM copy rules:
- Explicitly acknowledge the action — say they liked it / reposted it. Don't be vague.
- Use "Thanks" not "Appreciate"

### DM conversation funnel (`poll_inbound_dms`)

When a fan replies to any outreach DM (like/repost initial DM, or after a public reply DM pull), `poll_inbound_dms` classifies their message and routes to the appropriate generator:

| Fan message | Generator | Stage written |
|---|---|---|
| Subscriber mention | `generate_dm_subscriber_reply` — warm thanks, asks about their favourite content | `subscriber` |
| `buying_signal` or `curious` (first time) | `generate_dm_funnel_reply` with discount code | `converted` |
| `buying_signal` or `curious` (discount already sent) | `generate_dm_funnel_reply` without code, steers to site | `engaged` |
| Casual / neutral | `generate_conversation_reply` (CTA suppressed) | `engaged` |

`generate_dm_funnel_reply` uses the same `_score_thread_signal` scoring as `generate_conversation_reply` — tone and register mirror the fan's energy level (low/medium/high tiers). The promotional offer is woven in at that register, not as a sales pivot.

The discount fires intent-driven — most fans hit it on the 2nd or 3rd exchange. `discount_sent: true` in Firestore prevents a second code being sent to the same fan.

Anti-spam rules (all types):
- Same notification URI already seen → skip (dedup via Firestore `seen_events`)
- Handle already has a `conversations` doc → `_already_dmed()` skips it (one outreach DM per handle ever)
- `human_handoff` flag set → automated replies silenced until manually cleared

---

## Notification Scope (extensibility)

Currently handles: `reply`, `like`, `repost`, `follow`

To add `mention` or other types: add a handler to the `HANDLERS` dict in `bluesky/reply/poller.py`. No refactor needed.

```python
HANDLERS = {
    "reply": "_handle_reply",     # active
    # "mention": handle_mention,  # future
    # "quote": handle_quote,      # future
}
```

---

## Environment Variables

See `bluesky/reply/.env.example` for the full list. Required:

```
BLUESKY_HANDLE=
BLUESKY_APP_PASSWORD=        # bsky.app → Settings → App Passwords
ANTHROPIC_API_KEY=
BRANDVOICE_PATH=             # absolute path to SX_Instructions.md
GOOGLE_CLOUD_PROJECT=sx-platform
```

Creator detection (for Sean's account):
```
CREATOR_DETECTION_MUTUAL_FOLLOW=true
CREATOR_DETECTION_BIO=false
CREATOR_DETECTION_FOLLOWER_COUNT=false
COLLAB_DM_THRESHOLD=20000
```

Optional:
```
DISCOUNT_OFFER=              # legacy fallback — prefer FAN_DISCOUNT_CODE
MAX_DISCOUNTS_PER_DAY=5
MAX_CONVERSATION_DEPTH=3     # follow-up rounds before forcing DM pull
DAILY_DM_CAP=50              # max outreach DMs per day (fan_pipeline.py)
DAILY_COMMENT_CAP=50         # max comments posted per day (comment_engine.py)
DM_ENABLED=true              # set to false to disable proactive like/repost DMs
FAN_DISCOUNT_URL_DM=         # tracking URL for DM conversation CTA (falls back to FAN_DISCOUNT_URL_REPLY)
STATE_PATH=                  # override default state.json location
DM_STATE_PATH=               # override default dm_state.json location
```

**Cloud Functions** — `STATE_PATH` and `DM_STATE_PATH` are automatically set to `/tmp/state.json` and `/tmp/dm_state.json` via `deploy.sh`. The `/tmp` directory is the only writable path in Cloud Run-backed Gen2 functions; it is ephemeral per container. Full Firestore migration of these files is tracked as future work.

---

## Module Map

### Existing (reply bot)
| Path | Purpose |
|---|---|
| `brandvoice/brandvoice-template-v1.md` | BrandVoice schema (versioned) |
| `bluesky/shared/bluesky_client.py` | atproto wrapper — auth (session-persistent), notifications, post, DM send/list |
| `bluesky/reply/reply_generator.py` | Claude API — reply generation, classification, subscriber detection |
| `bluesky/reply/dm_generator.py` | Claude API — proactive DM generation for likes/reposts; adaptive conversation replies with signal-gated mirroring and CTA injection |
| `bluesky/reply/creator_classifier.py` | Creator detection logic and peer routing constants |
| `bluesky/reply/state_manager.py` | Reply state — dedup, blocklist, depth, discount cap (JSON, Firestore migration pending) |
| `bluesky/reply/dm_manager.py` | DM state — per-handle user-type classification cache + engagement watermark (JSON) |
| `bluesky/reply/poller.py` | Main loop — poll → classify → route → generate → post/DM |
| `bluesky/reply/scan_and_test.py` | Dev tool — test reply flows against real or simulated data |
| `bluesky/reply/admin.py` | CLI overrides |

### Engagement system
| Path | Purpose |
|---|---|
| `bluesky/shared/rate_limiter.py` | Centralized Bluesky API rate limit manager |
| `bluesky/shared/firestore_client.py` | Firestore connection wrapper |
| `bluesky/engagement/fan_pipeline.py` | Immediate DM send (`send_engagement_dm`), follow queue, batch executor, inbound DM polling |
| `bluesky/engagement/handoff.py` | Human handoff detection and flag management |
| `bluesky/engagement/discovery.py` | Audience discovery — starter packs + follower graph |
| `bluesky/engagement/comment_engine.py` | Comment scoring, generation, queue executor |
| `functions/main.py` | Cloud Function entry points (9 functions, incl. cleanup) |
| `deploy.sh` | Deploy all Cloud Functions via gcloud |
| `scheduler.sh` | Create/update all Cloud Scheduler jobs |
| `bluesky/web/index.html` | Admin web UI (deferred) |

---

## Development Phases

| Phase | Status | Description |
|---|---|---|
| 0 | ✓ | Repo scaffold + BrandVoice template |
| 1 | ✓ | Reply bot — dry-run working end-to-end |
| 2 | ✓ | Tone iteration: question replies, DM pull, discount logic, creator/peer register |
| 3 | ✓ | Live local testing (reply bot goes live) |
| 4 | ✓ | Firebase foundation — Firestore connection, rate limit manager |
| 5 | ✓ | Engagement system: enhanced fan pipeline (follow handler, DM queue, batch executor, reply delay) |
| 6 | ✓ | Human handoff detection |
| 7 | ✓ | Audience discovery (starter packs + follower graph) |
| 8 | ✓ | Comment engine (post scoring, queue, executor) |
| 9 | ✓ | Cloud Functions + Cloud Scheduler deploy |
| 10 | ✓ | Audit: dead code removal, conflict fixes, limit hardening, cleanup CF |
| 11 | ✓ | Adaptive DM replies: signal-gated mirroring, CTA injection after 2 exchanges; brand voice versatility fixes |
| 12 | ✓ | Initiation test fixes: reply-delay skip in Cloud Functions, cleanup-stale-docs UnboundLocalError, execute-dm-batch batch_size override, Firestore composite indexes |

Full spec: `BLUESKY_ENGAGEMENT_SPEC.md`

### Post-deploy design notes

**Session persistence** (`bluesky_client.py`)
`BlueskyClient.login()` stores the atproto session string in Firestore `_system/bluesky_session` after every login. On the next invocation it restores from that doc instead of calling `createSession`. The SDK auto-refreshes the access token (~2hr lifetime) using the stored refresh token (~90 days) without counting against the 300/day `createSession` quota. A full `createSession` only occurs on first run or after ~90-day refresh token expiry.

**Inbound DM polling** (`fan_pipeline.py → poll_inbound_dms`)
Uses `chat.bsky.convo.listConvos` (sorted by recent activity, includes `unread_count` per convo) instead of streaming all Firestore conversations. Only conversations with `unread_count > 0` are processed. Pagination stops as soon as a full page has no unread entries — unread convos cluster at the top of the list.

**`human_handoff` flag behavior**
The flag is a per-reply guard, not a polling filter. When a handed-off conversation has a new fan message, it appears in `listConvos` as unread, the flag is checked, and we skip without replying — leaving the message unread in Bluesky so a human operator can see it in the app. Use `python bluesky/reply/admin.py clear-handoff @handle` to resume automated replies.

**Inbound DM polling — local vs Cloud Functions**
`poll_inbound_dms()` is called in two places:
- **Local mode** (`poller.py --interval 60`): called directly in the `main()` loop after each `run_once()`.
- **Cloud Functions**: called by the dedicated `poll-inbound-dms` CF (every 5 min). The `poll-notifications` CF calls only `run_once()` and does NOT call `poll_inbound_dms` — the separate CF handles it. This prevents double-processing.

**Rate limiter — read vs write tracking**
`check_read()` is tracked in-memory per process (reads are IP-based; Firestore coordination adds no value and would cost ~20K extra ops during follower graph runs).
`check_write()` and `seconds_until_next_write()` are tracked in Firestore `_system/rate_state` so the global 4-minute write window is enforced across all Cloud Functions.

**Write window now covers replies**
`post_reply()` in `poller.py` now goes through `check_write()` / `seconds_until_next_write()`, the same as DM sends and comment posts. All three write paths share the global 4-minute window.

**Reply posting delay — local only**
`poller.py` applies a `random.uniform(90, 600)s` human-pacing delay before posting replies, and waits for the write window before calling `check_write`. Both sleeps are gated on `not os.environ.get("K_SERVICE")` — Cloud Run sets `K_SERVICE` automatically, so the delays only fire in local mode. In Cloud Functions, if the write window isn't clear, `check_write` raises `RateLimitError` and the reply is skipped (stays unseen, retried on the next 5-min invocation).

**Immediate like/repost DMs** (`fan_pipeline.py → send_engagement_dm`)
Called directly from `poll-notifications` when a like or repost is detected. Sends the DM in the same invocation — no queue, no stagger. Respects the global 4-min write window (`check_write`) and the one-outreach-per-handle rule (`_already_dmed`). Creates the `conversations` doc and logs the message to `messages` subcollection identically to the batch path.

**DM batch (follows only)**
`execute_dm_batch` (Cloud Functions) uses `batch_size=7`. With 8–20 min stagger per send and a 3600s timeout, 7 items is the practical maximum. Only follow-triggered DMs go through this path. Items not sent remain pending and are picked up at the next 4-hour invocation.

`batch_size` can be overridden via JSON request body: `{"batch_size": 0}` exits immediately after auth/Firestore checks with no DMs sent. `initiation_test.sh` uses this to verify connectivity without blocking on the live queue.

**Cleanup job**
`cleanup-stale-docs` CF runs weekly (Sunday 4am). Deletes `seen_events` > 7 days old, and `sent`/`skipped`/`posted` records from `dm_queue` and `comment_queue` older than 30 days. Pending items are never deleted.

Requires two Firestore composite indexes (created once at setup — see B2 in README):
- `dm_queue`: `(status ASC, created_at ASC)`
- `comment_queue`: `(status ASC, queued_at ASC)`

**Adaptive DM conversation replies** (`dm_generator.py → generate_conversation_reply`)
Before generating a reply, all fan messages in the thread are scored across five dimensions (volume, specificity, register, disclosure, complexity) using `_score_thread_signal`. The composite score (0–10) selects one of three instruction tiers:
- **Low (0–3):** Default Sean voice; reply ends with a question or observation designed to coax more signal from the fan.
- **Medium (4–6):** Match the fan's energy level and temperature without fully adopting their vocabulary.
- **High (7–10):** Full mirroring — register, vocabulary, pace, and emotional temperature all adapt to the fan. Voice is always Sean's.

**DM conversation CTA gating**
`exchange_count` (fan messages in thread history) is computed in `poll_inbound_dms` and passed to `generate_conversation_reply`. When `exchange_count >= 2` and `FAN_DISCOUNT_URL_REPLY` (or `FAN_DISCOUNT_URL_DM`) is set, a natural CTA is injected into the reply prompt. The consecutive-unanswered guard (`last_sender == my_did → skip`) ensures the CTA only ever fires into an active exchange.

### Phase 3 checklist (before going live)

**Tone sign-off**
- [ ] Run `python bluesky/reply/scan_and_test.py` — confirm fan reply tone is right
- [ ] Run `python bluesky/reply/scan_and_test.py --creator` — confirm peer register tone is right
- [ ] Adjust prompts in `reply_generator.py` if needed, repeat until satisfied

**Environment**
- [ ] Verify `CREATOR_DETECTION_MUTUAL_FOLLOW=true` in `.env`
- [ ] Set `DISCOUNT_OFFER` in `.env` if using discounts live
- [ ] Confirm `FIRESTORE_EMULATOR_HOST` is **not** present in `.env`

**Go live**
- [ ] Delete or clear `state.json` (removes all dry-run dedup entries)
- [ ] Run `python bluesky/reply/poller.py --once` — one real cycle, replies will post
- [ ] Spot-check the posted replies on Bluesky
- [ ] Run `python bluesky/reply/admin.py status` — confirm state looks right
- [ ] If tone needs adjustment: `python bluesky/reply/admin.py pause-all`, tweak, resume

**Continuous mode**
- [ ] Run `python bluesky/reply/poller.py --interval 60` to go fully live
