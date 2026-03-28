# sx-templates

Automated Bluesky engagement for creators — reply threading, proactive DMs, strategic commenting, and audience discovery. Powered by Anthropic Claude and your own brand voice.

**Stack:** Python 3.11 · Anthropic Claude · Bluesky atproto · Google Cloud (Firestore, Cloud Functions, Cloud Scheduler)

---

## What It Does

The system has four components that layer on top of each other:

| Component | What it does | Runs where |
|---|---|---|
| **Reply bot** | Reads notifications, classifies fans/creators, replies in-thread, steers toward DMs | Local or Cloud |
| **Fan pipeline** | Sends immediate DMs on likes/reposts; queues and batches DMs on follows | Cloud |
| **Comment engine** | Monitors target accounts, scores posts, generates and posts on-brand comments | Cloud |
| **Audience discovery** | Scans starter packs and follower graphs to rank target accounts by tier | Cloud |

All text generation routes through an Anthropic Claude prompt loaded with your brand voice. No replies, DMs, or comments are posted without a generated draft.

```
Bluesky API
    │
    ▼
poller.py  ──── classify user ──────────────────────────────┐
    │                                                        │
    ├── fan reply ──► nudging question                       │
    │                 → classify intent                      │
    │                 → DM pull (+ optional discount)        │
    │                                                        │
    ├── creator reply ──► peer routing (collab / warm ack)  │
    │                                                        │
    ├── like / repost ──► immediate DM (fan_pipeline.py)    │
    │                                                        │
    └── follow ──► queue DM (execute-dm-batch CF)           │
                                                             │
Claude API ◄─────────────────────────────────────────────────┘
(brand voice loaded from BRANDVOICE_PATH)
```

For the full engagement system spec, see [`BLUESKY_ENGAGEMENT_SPEC.md`](BLUESKY_ENGAGEMENT_SPEC.md).

---

## Prerequisites

- Python 3.11+
- A Bluesky account with an App Password (`bsky.app → Settings → App Passwords`)
- An [Anthropic API key](https://console.anthropic.com)
- **For Module B (cloud features only):** a Google Cloud project with billing enabled

---

## Module A — Reply Bot (local, no GCP needed)

The reply bot runs entirely on your machine. It polls Bluesky for new notifications, classifies each one, and replies using your brand voice. No cloud infrastructure required.

### A1. Installation

```bash
git clone <your-fork-url>
cd sx-templates
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp bluesky/reply/.env.example bluesky/reply/.env
```

### A2. BrandVoice Configuration

Brand voice drives every Claude-generated reply and DM. The schema lives at [`brandvoice/brandvoice-template-v1.md`](brandvoice/brandvoice-template-v1.md).

**To set up your brand voice:**

1. Copy the template to a file outside this repo (keep your values private):
   ```bash
   cp brandvoice/brandvoice-template-v1.md ~/my-brandvoice.md
   ```
2. Open `~/my-brandvoice.md` and fill in every section:
   - **§1 Identity** — your name, handle, persona, and core pillars
   - **§2 Voice & Register** — your philosophy, modes, and POV
   - **§3 Lexicon** — approved vocab, banned words, punctuation rules
   - **§4 Structural Rules** — fragment length, rhythm, verb variety
   - **§5 Content Rules** — global always/never rules
   - **§6 Platform Extensions** — Bluesky-specific tone, reply length, thread behavior
   - **§7 Archetypes** *(optional)* — audience segments and how to open for each

3. Set `BRANDVOICE_PATH` in your `.env` to the absolute path:
   ```
   BRANDVOICE_PATH=/Users/you/my-brandvoice.md
   ```

### A3. Environment Variables

Open `bluesky/reply/.env` and fill in the required fields:

| Variable | Required | Purpose |
|---|---|---|
| `BLUESKY_HANDLE` | Yes | Your Bluesky handle, e.g. `you.bsky.social` |
| `BLUESKY_APP_PASSWORD` | Yes | App password from Bluesky settings |
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key |
| `BRANDVOICE_PATH` | Yes | Absolute path to your filled brand voice file |
| `CREATOR_DETECTION_MUTUAL_FOLLOW` | Recommended | `true` if you only follow creators — most reliable signal |
| `CREATOR_DETECTION_BIO` | Optional | `true` to detect creators by bio keywords |
| `CREATOR_DETECTION_FOLLOWER_COUNT` | Optional | `true` to detect creators by follower count (≥ `CREATOR_FOLLOWER_THRESHOLD`) |
| `FAN_DISCOUNT_CODE` | Optional | Discount code to include in qualifying DM pulls |
| `FAN_DISCOUNT_URL_REPLY` | Optional | Tracking URL for reply-thread DM pulls |
| `DM_ENABLED` | Optional | Set `false` to disable proactive like/repost DMs (default `true`) |
| `MAX_CONVERSATION_DEPTH` | Optional | Follow-up rounds before forcing a DM pull (default `3`) |
| `MAX_DISCOUNTS_PER_DAY` | Optional | Daily cap on discount sends (default `5`) |

See [`.env.example`](bluesky/reply/.env.example) for the full list including audience discovery and GCP variables.

### A4. Test Without Posting

Run a full cycle against real Bluesky data. Replies and DMs are generated and printed but **nothing is posted**.

```bash
python bluesky/reply/poller.py --dry-run --once
```

You'll see each notification fetched, the user type classification, the generated reply text, and whether it would be posted or skipped. Iterate on your brand voice file until the tone is right.

**Development tool** — target specific posts or flows:

```bash
# Test against posts from 3 days ago
python bluesky/reply/scan_and_test.py --days-ago 3

# Test discount DM pull flow
python bluesky/reply/scan_and_test.py --discount "20% off — DM me for the link"

# Test creator/peer routing (scans last 25 posts for creator replies)
python bluesky/reply/scan_and_test.py --creator
```

### A5. Go Live

When the tone looks right, clear any dry-run dedup state and go live:

```bash
# Optional: clear state from dry-run cycles so real notifications aren't skipped
rm bluesky/reply/state.json

# One real cycle — replies will post
python bluesky/reply/poller.py --once

# Spot-check the replies on Bluesky, then run continuously
python bluesky/reply/poller.py --interval 60
```

### A6. Admin Controls

```bash
python bluesky/reply/admin.py status           # show bot state and daily counters
python bluesky/reply/admin.py pause-all        # pause all replies
python bluesky/reply/admin.py resume           # resume from pause
python bluesky/reply/admin.py pause-user @handle   # skip a specific handle temporarily
python bluesky/reply/admin.py block-user @handle   # permanently skip a handle
python bluesky/reply/admin.py unblock-user @handle
python bluesky/reply/admin.py clear-handoff @handle  # resume automated replies after human handoff
```

### A7. Reply Logic

**Fan routing** (everyone who is not a creator, studio, or themed account):

```
classify_post_type(root_post)

if personal / casual post:
    → friendly reply, no funnel

if promotional / content post:
    first reply      → nudging question (steers toward buying signals)
    follow-up        → classify fan intent
        buying_signal or curious  → DM pull (+ discount if qualified)
        casual / no signal        → another nudging question
        depth ≥ MAX_DEPTH (3)     → force DM pull
```

**Subscriber guard:** if the reply mentions being an existing subscriber, send a warm thank-you and exit — no nudge, no DM pull.

**Creator / peer routing:** mutual-follow and/or bio/follower signals determine if a replier is a peer. Peers with ≥ 20k followers get collab DM pulls or warm peer acknowledgment. Peers with < 20k followers get brief, warm replies with no DM.

**Discount rules:** a discount is included in a DM pull when fan intent is `buying_signal` (not just `curious`), post type is `promotional`, `FAN_DISCOUNT_CODE` is set, and the daily cap has not been hit.

---

## Module B — Engagement System (Google Cloud)

The engagement system adds proactive outreach, strategic commenting, and audience discovery on top of the reply bot. All components run as Cloud Functions triggered by Cloud Scheduler.

### B1. What This Adds

- **Proactive DMs** — likes and reposts trigger an immediate DM; follows are queued and batched (50/day cap)
- **Comment engine** — scans Tier 1/2 target accounts every 15 min, scores posts by engagement, posts on-brand comments (50/day cap, 20–30 min cadence)
- **Audience discovery** — weekly starter pack scan + nightly follower graph analysis assigns accounts to Tier 1/2/3 as future comment and DM targets
- **Inbound DM polling** — active conversations are checked every 5 min; human handoff flag silences automation when needed

### B2. GCP Setup

**Enable APIs:**
```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable \
  cloudfunctions.googleapis.com \
  cloudscheduler.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com
```

**Create Firestore database** (native mode, not Datastore):
```bash
gcloud firestore databases create --database=YOUR_DATABASE_NAME --location=us-central1
```

**Create Firestore composite indexes** (required by `cleanup-stale-docs`):
```bash
gcloud firestore indexes composite create \
  --database=YOUR_DATABASE_NAME \
  --collection-group=dm_queue \
  --field-config=field-path=status,order=ascending \
  --field-config=field-path=created_at,order=ascending

gcloud firestore indexes composite create \
  --database=YOUR_DATABASE_NAME \
  --collection-group=comment_queue \
  --field-config=field-path=status,order=ascending \
  --field-config=field-path=queued_at,order=ascending
```

**Create secrets in Secret Manager:**
```bash
gcloud secrets create bluesky-app-password --replication-policy automatic
gcloud secrets create anthropic-api-key    --replication-policy automatic
gcloud secrets create brandvoice-content   --replication-policy automatic

# Add values
echo -n "your-app-password" | gcloud secrets versions add bluesky-app-password --data-file=-
echo -n "your-api-key"      | gcloud secrets versions add anthropic-api-key    --data-file=-
cat /path/to/your-brandvoice.md | gcloud secrets versions add brandvoice-content --data-file=-
```

### B3. Configure deploy.sh

Before deploying, open [`deploy.sh`](deploy.sh) and update the `ENV_FILE` YAML block with your values:

```yaml
BLUESKY_HANDLE: you.bsky.social
GOOGLE_CLOUD_PROJECT: your-project-id
FIRESTORE_DATABASE: your-database-name
DISCOVERY_CREATOR_HANDLE: you.bsky.social
DISCOVERY_DOMAIN_KEYWORDS: "your niche,keywords"
DISCOVERY_DOMAINS: "domain1,domain2"
# ... other values
```

The `BLUESKY_APP_PASSWORD`, `ANTHROPIC_API_KEY`, and brand voice content are injected from Secret Manager — do not put them in the YAML.

### B4. Deploy

```bash
bash deploy.sh      # deploys 11 Cloud Functions
bash scheduler.sh   # creates/updates all Cloud Scheduler jobs
```

**Cloud Functions and their schedules:**

| Function | Schedule | Purpose |
|---|---|---|
| `poll-notifications` | every 5 min | Reply to thread notifications; send immediate like/repost DMs |
| `poll-inbound-dms` | every 5 min | Check active DM conversations for fan replies |
| `scan-comment-targets` | every 15 min | Score Tier 1/2 posts and queue qualifying ones for commenting |
| `execute-comment` | every 20 min | Post the next queued comment (respects write window + daily cap) |
| `process-dm-queue` | every 2 hours | Pre-generate DM content for queued follow interactions |
| `execute-dm-batch` | every 4 hours | Send batched follow DMs with 8–20 min stagger; pass `{"batch_size":0}` for a quick connectivity check |
| `follower-graph-prefetch` | Saturday 1am | Fetch all followers of your account; filter by quality signals |
| `follower-graph-slot-0…4` | Nightly 2–5am | Process one 2000-fan slice per slot; count shared followees |
| `follower-graph-score` | Nightly 5:30am | Assign Tier 1/2/3 to accounts based on discovery sources |
| `starter-pack-discovery` | Sunday 3am | Search starter packs by keyword; score and store members |
| `cleanup-stale-docs` | Sunday 4am | Delete `seen_events` > 7 days; remove old sent/skipped queue items |

### B5. Monitoring and Human Handoff

**Activity logs** are written to Firestore `_system/activity_log` after every function run — timestamp, function name, items processed, errors.

**Human handoff** is triggered automatically when a fan's DM conversation warrants a human response (e.g. complaints, complex questions, payment issues). When the flag is set, automated replies are silenced for that handle and the conversation is left unread in the Bluesky app for a human operator.

To resume automated replies after handling the conversation:
```bash
python bluesky/reply/admin.py clear-handoff @handle
```

---

## Module C — Developer Reference

### C1. Module Map

**Shared**

| Path | Purpose |
|---|---|
| `bluesky/shared/bluesky_client.py` | atproto SDK wrapper — session-persistent login, notifications, post, DM send/list |
| `bluesky/shared/rate_limiter.py` | Centralized API rate limit manager (read: in-memory; write: Firestore-coordinated) |
| `bluesky/shared/firestore_client.py` | Firestore connection wrapper |
| `bluesky/shared/activity_logger.py` | Logs function run metrics to Firestore `_system/activity_log` |

**Reply bot**

| Path | Purpose |
|---|---|
| `bluesky/reply/poller.py` | Main loop — poll → classify → route → generate → post/DM |
| `bluesky/reply/reply_generator.py` | Claude API — reply generation, fan intent classification, subscriber detection |
| `bluesky/reply/dm_generator.py` | Claude API — proactive DM generation for likes/reposts/follows |
| `bluesky/reply/creator_classifier.py` | Creator detection logic and peer routing constants |
| `bluesky/reply/state_manager.py` | Reply state — dedup, blocklist, depth, discount cap (JSON file) |
| `bluesky/reply/dm_manager.py` | DM state — per-handle user-type classification cache and engagement watermark (JSON file) |
| `bluesky/reply/scan_and_test.py` | Dev tool — test reply flows against real or simulated data without posting |
| `bluesky/reply/admin.py` | CLI admin controls |

**Engagement system**

| Path | Purpose |
|---|---|
| `bluesky/engagement/fan_pipeline.py` | Immediate DM send, follow queue, batch executor, inbound DM polling |
| `bluesky/engagement/comment_engine.py` | Post scoring, comment generation, queue executor |
| `bluesky/engagement/discovery.py` | Starter pack search + follower graph analysis → Tier 1/2/3 accounts |
| `bluesky/engagement/handoff.py` | Human handoff detection and flag management |

**Cloud Functions**

| Path | Purpose |
|---|---|
| `functions/main.py` | HTTP-triggered entry points for all 11 Cloud Functions |
| `deploy.sh` | Deploy all functions via `gcloud` |
| `scheduler.sh` | Create/update all Cloud Scheduler jobs |

**Brand voice**

| Path | Purpose |
|---|---|
| `brandvoice/brandvoice-template-v1.md` | Versioned schema — structure only, no creator values |

### C2. Extension Points

**Adding notification types**

The reply bot handles `reply`, `like`, `repost`, and `follow` out of the box. To add `mention` or `quote`:

```python
# bluesky/reply/poller.py
HANDLERS = {
    "reply": "_handle_reply",
    "mention": handle_mention,   # add your handler here
}
```

No other changes are needed — the dispatch loop iterates over `HANDLERS`.

**Adding user type detection**

To add manual handles for studio or themed account routing, set these in `.env`:

```
STUDIO_HANDLES=men.bsky.social,nakedsword.bsky.social
THEMED_HANDLES=geekgooner.bsky.social
```

To add a new user type beyond the existing four (`fan`, `creator`, `themed`, `studio`), add detection logic in `creator_classifier.py` and a routing branch in `poller.py`.

**Upgrading BrandVoice to v2**

When a new template version is released:
1. Diff `brandvoice/brandvoice-template-v1.md` against `brandvoice-template-v2.md`
2. Update your filled instance file to conform to the new structure
3. Bump `template_version` in your instance's frontmatter

### C3. State Architecture

**Reply bot** (JSON files, Firestore migration pending):

| File | Contents |
|---|---|
| `bluesky/reply/state.json` | `replied_posts`, `my_reply_uris`, `dm_pulls_by_root`, `conversation_depth`, `daily_discounts`, `blocked_users`, `paused_users`, `bot_status` |
| `bluesky/reply/dm_state.json` | Per-handle classification cache (`user_type`, `follower_count`, `classified_at`) and `last_checked_at` watermark |

Override file locations with `STATE_PATH` and `DM_STATE_PATH` env vars. In Cloud Functions these are set to `/tmp/` automatically — state is ephemeral per container.

**Engagement system** (Firestore):

| Collection | Purpose |
|---|---|
| `target_accounts` | Ranked anchor accounts by domain and tier (1–3) |
| `engagement_events` | Raw notification log — reply, like, repost, follow |
| `dm_queue` | Outbound DMs awaiting batch send (follows only) |
| `conversations` | Per-fan DM thread state — stage, handoff flag, one-outreach-per-handle guard |
| `messages` | Full message history per conversation (subcollection of `conversations`) |
| `comment_queue` | Generated comments awaiting posting |
| `seen_events` | Notification URI dedup log (cleaned up weekly) |
| `_system/bluesky_session` | Persisted atproto session string — avoids `createSession` quota |
| `_system/rate_state` | Global write window state shared across all Cloud Functions |
| `_system/activity_log` | Per-function run metrics |

**Session persistence:** `BlueskyClient.login()` stores the atproto session in Firestore after every login and restores it on the next invocation. The SDK auto-refreshes the access token (~2hr lifetime) using the stored refresh token (~90 days). A full `createSession` only fires on first run or after ~90-day expiry — well within the 300/day quota.

### C4. Rate Limiting

The Bluesky API enforces a global write rate limit across all your Cloud Functions. The rate limiter in `bluesky/shared/rate_limiter.py` coordinates this:

- **Reads** are tracked in-memory per process (IP-based; Firestore coordination adds cost with no benefit)
- **Writes** are tracked in Firestore `_system/rate_state` — a global 4-minute window shared across all Cloud Functions

All three write paths — `post_reply()`, DM sends, and comment posts — call `check_write()` before executing. If the window is not clear, `seconds_until_next_write()` returns the wait time and the function exits early; the item remains queued for the next invocation.
