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
        casual / no signal        → fan_discount_pull (if < 2 pulls on thread)
                                    OR another nudging question
        depth >= MAX_DEPTH (3)    → force DM pull
    after DM pull → Firestore conversations doc created for the fan

25% of fan replies:
    → friendly reply only, no funnel
```

**`fan_discount_pull` action:** Replaces the second nudge when the thread has had fewer than 2 DM pulls so far. Posts a public reply in Sean's voice mentioning the discount and inviting the fan to DM — no code or URL is exposed publicly. Simultaneously queues a `comment_exchange` DM for the commenter (subject to active hours + bot scorer). Up to 2 discount pulls per root thread URI (`state.get_dm_pulls(root_uri)` tracks count).

### Subscriber guard
Before any fan routing: if the reply mentions being an existing subscriber/member, send a warm thank-you and exit — no nudging question, no DM pull, no discount.

### Discount rules

Discounts are offered **only inside DM conversations** — the public `fan_discount_pull` reply mentions the discount exists and invites a DM, but never exposes the code or URL publicly.

**DM conversation discount** (`poll_inbound_dms`):

Two triggers, checked in order:
1. **First-reply discount** — fan's very first reply to any outreach DM (`exchange_count == 0`) and `discount_sent` is not already set → offer discount immediately, regardless of intent classification.
2. **Intent-based discount** — fan's DM is classified as `buying_signal` or `curious` (fires on subsequent exchanges if discount not yet sent).

Both require:
- `FAN_DISCOUNT_CODE` env var is set (falls back to legacy `DISCOUNT_OFFER`)
- `discount_sent` flag not already set on the conversation Firestore doc (one discount per fan ever)
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
| `engagement_events` | Per-interaction log — reply, like, repost, follow, comment, post. Fields: `type`, `handle`, `post_uri`, `direction`, `reply_type`, `interaction_subtype`, `fan_intent`, `mirror_tier`, `post_type_classification`, `token_usage_input`, `token_usage_output`, `model`, `created_at`. `type="post"` events also include `post_text` (first 300 chars); written by `_snapshot_my_posts()` in `poll_inbound_dms` every 5 min — powers the Posts markers on the Conversion graph. |
| `api_cost_events` | Per-Claude-call cost log — `provider`, `model`, `input_tokens`, `output_tokens`, `cost_usd`, `call_type`, `created_at`. Cleaned up after 90 days. |
| `dm_queue` | Outbound DMs awaiting batch send |
| `conversations` | Per-fan DM thread state and stage (`warm` → `engaged` / `converted` / `subscriber`; `dm_pull_sent` for public-reply-to-DM path). Key fields: `trigger_context` (like/repost/follow/reply_dm_pull — source of the outreach), `stage`, `discount_sent` (bool), `discount_sent_at` (ISO timestamp — written alongside `discount_sent`; used to bucket Effectiveness by when the discount was sent, not when the conversation was created), `fan_handle`, `created_at`. **Primary source for `/api/dm-effectiveness`** — attribution is by handle via `trigger_context`, no cross-collection join needed. Handoff fields: `human_handoff` (bool), `handoff_reason`, `pending_manual_reply` (string set by dashboard resolve), `has_pending_manual_reply` (bool index for CF query). |
| `messages` | Full message history per conversation (subcollection) |
| `comment_queue` | Generated comments awaiting posting |
| `seen_events` | Notification URI dedup log |
| `_system/settings` | Writable bot settings — caps, detection flags, discount config. Read by bot on startup; overrides env var defaults. |
| `_system/brand_voice` | Firestore-stored brand voice doc. Bot reads this first (if newer than file); falls back to `BRANDVOICE_CONTENT` env var then `BRANDVOICE_PATH` file. |
| `_system/follower_snapshots` | Nightly follower count snapshots `{ date, count }` written by `snapshot-follower-count` CF |
| `_system/post_cache` | Bluesky post text + image URL cache, keyed by URI (used by dashboard content page) |
| `_system/insights_cache` | Python-computed insights bar HTML, 1hr TTL |
| `_system/tone_review_queue` | Surfaced tone review items — edge cases + samples. Refreshed on-demand via `POST /api/tone-review/refresh` and nightly by Cloud Scheduler. |
| `_system/tone_review_feedback` | Approve/flag history from tone review sessions |
| `_system/classifier_stats` | Per-classifier accuracy history (`intent`, `posttype`, `subguard`, `handoff`) |
| `_system/classifier_labels` | Individual labeling session answers |

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

**Active hours gate:** All outgoing actions (public replies, DMs, inbound DM replies) are gated to **7am–10pm America/Los_Angeles**. `is_active_hours()` in `rate_limiter.py` checks this. Outside active hours: `run_once()` returns early, `poll_inbound_dms()` returns early, `execute_engagement_dm_queue()` returns early.

**Likes and reposts are queued** via `queue_dm()` in `poll-notifications`, then drained by the dedicated `execute-engagement-dms` CF (every 5 min). This decouples the 4-min public write window from DM sends, allowing ~4 DMs per 5-min cycle instead of 1.

Queue-time filters applied in `_handle_engagement` (poller.py):
- Post must be ≤ 1hr old at queue time (`post.record.created_at`)
- Fan-type accounts: `bot_score(profile) < 5` (6-signal scorer — see below)
- `dry_run` guard prevents queuing in test mode

Send-time filters applied in `execute_engagement_dm_queue`:
- Engagement must be ≤ 1hr old at send time (`interaction_at` field)
- `_already_dmed(handle)` one-outreach-per-handle rule
- 90–600s human pacing between sends (random stagger, skipped before the first send per cycle)
- 60s DM write window (`check_dm_write()`) — independent of the 4-min public write window

**Bot scorer for fans** (`creator_classifier.py → bot_score(profile)`):
Returns int 0–11; skip DM queue if score ≥ 5 (`BOT_SCORE_SKIP = 5`).
| Signal | Points |
|---|---|
| followers ≤ 50 | +3 |
| posts ≤ 5 | +2 |
| following > 2000 and followers < 200 | +2 |
| follow ratio (followers/following) < 0.05 | +2 |
| no avatar | +1 |
| no display name | +1 |

**Follows** are still queued via `queue_dm` / `execute-dm-batch` — no urgency.

**`comment_exchange` DMs** are queued after a `fan_discount_pull` reply (see Fan reply routing). They drain via the same `execute-engagement-dms` CF.

| Type | Like | Repost |
|---|---|---|
| Fan | Thank-you DM (queued) | Thank-you DM, higher energy (queued) |
| Creator | Skip | Peer-register thank-you DM (queued) |
| Themed | Skip | Playful, niche-aware thank-you DM (queued) |
| Studio | Skip | Simple professional thank-you DM (queued) |

DM copy rules:
- Explicitly acknowledge the action — say they liked it / reposted it. Don't be vague.
- Use "Thanks" not "Appreciate"

### DM conversation funnel (`poll_inbound_dms`)

When a fan replies to any outreach DM (like/repost initial DM, or after a public reply DM pull), `poll_inbound_dms` classifies their message and routes to the appropriate generator:

| Fan message | Generator | Stage written |
|---|---|---|
| Subscriber mention | `generate_dm_subscriber_reply` — warm thanks, asks about their favourite content | `subscriber` |
| First-ever reply (`exchange_count == 0`) and no discount sent | `generate_dm_funnel_reply` with discount code — fires immediately regardless of intent | `converted` |
| `buying_signal` or `curious` (first time, after first reply) | `generate_dm_funnel_reply` with discount code | `converted` |
| `buying_signal` or `curious` (discount already sent) | `generate_dm_funnel_reply` without code, steers to site | `engaged` |
| Casual / neutral | `generate_conversation_reply` (CTA suppressed) | `engaged` |

Routing order: subscriber check → first-reply discount → intent routing.

`generate_dm_funnel_reply` uses the same `_score_thread_signal` scoring as `generate_conversation_reply` — tone and register mirror the fan's energy level (low/medium/high tiers). The promotional offer is woven in at that register, not as a sales pivot.

`discount_sent: true` in Firestore prevents a second code being sent to the same fan.

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
DAILY_COMMENT_CAP=50         # max comments posted per day (comment_engine.py) — currently set to 0 in deploy.sh (commenting paused)
DM_ENABLED=true              # set to false to disable proactive like/repost DMs
FAN_DISCOUNT_URL_DM=         # tracking URL for DM conversation CTA (falls back to FAN_DISCOUNT_URL_REPLY)
STATE_PATH=                  # override default state.json location
DM_STATE_PATH=               # override default dm_state.json location
```

Dashboard (Phase 13):
```
DASHBOARD_SECRET=           # Bearer token for all dashboard API endpoints
MONTHLY_SPEND_CAP_USD=      # Optional budget cap; amber at 80%, red at 100%
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
| `bluesky/shared/rate_limiter.py` | Centralized rate limit manager — dual write windows: `check_write()` (4-min public window, Firestore-coordinated), `check_dm_write()` (60s DM window, independent); `is_active_hours()` (7am–10pm Pacific) |
| `bluesky/shared/firestore_client.py` | Firestore connection wrapper |
| `bluesky/shared/cost_calculator.py` | Anthropic cost calculation + `api_cost_events` writer (Phase 13) |
| `bluesky/reply/creator_classifier.py` | Creator detection logic, peer routing constants, `bot_score(profile)` (6-signal fan bot scorer, 0–11) |
| `bluesky/engagement/fan_pipeline.py` | Engagement DM queue (`execute_engagement_dm_queue` — like/repost/comment_exchange), follow batch executor, inbound DM polling with active hours + burst window; `send_engagement_dm` still used by the batch paths |
| `bluesky/engagement/handoff.py` | Human handoff detection and flag management |
| `bluesky/engagement/discovery.py` | Audience discovery — starter packs + follower graph |
| `bluesky/engagement/comment_engine.py` | Comment scoring, generation, queue executor |
| `functions/main.py` | Cloud Function entry points (12 functions, incl. `execute-engagement-dms`, cleanup + snapshot-follower-count) |
| `deploy.sh` | Deploy all Cloud Functions + Cloud Run dashboard API via gcloud |
| `scheduler.sh` | Create/update all Cloud Scheduler jobs |

### Web dashboard (Phase 13)
| Path | Purpose |
|---|---|
| `bluesky/web/mockups/sx-dashboard-v7.html` | Approved visual design — **do not modify** |
| `bluesky/web/specs/CLAUDE_CODE_HANDOFF.md` | Master build spec — 24-step implementation order |
| `bluesky/web/specs/DASHBOARD_SPEC.md` | All API endpoints mapped to Firestore sources |
| `bluesky/web/specs/BRAND_VOICE_SPEC.md` | Brand voice editor storage + push flow |
| `bluesky/web/specs/QUERY_AND_SPEND_SPEC.md` | Query bar (all pages) + spend tracking |
| `bluesky/web/api/main.py` | FastAPI backend — 33 endpoints, deployed to Cloud Run. Includes `GET /api/handoff/{handle}` (conversation detail for modal), `POST /api/handoff/{handle}/resolve` (save reply + clear/keep handoff flag), `GET /api/handles?q=` (typeahead for handles with logged activity), `GET /api/user/{handle}` (activity history + message thread), `POST /api/user/{handle}/dm` (queue a manual outbound DM via `pending_manual_reply`), `GET /api/errors` (per-function health breakdown, sorted errors → warnings → ok). Health system: `_FUNCTION_HEALTH_CONFIG` (12-function strategy table — `consecutive`, `any_today`, `any_occurrence`, `warning_only`), `_eval_fn_health()` (evaluates one function against its strategy), `_build_error_detail()` (drilldown row: reason, last-ok time, deduplicated error messages). `GET /api/health` `error_count_today` is now count of functions worth attention (not raw error docs); adds `has_warnings` bool. |
| `bluesky/web/api/requirements.txt` | FastAPI backend dependencies |
| `bluesky/web/dashboard.html` | Wired SPA — mockup with fetch() replacing mock data. Health bar: `errors N` is clickable; red when functions are alerting, amber when warnings only. Clicking toggles an inline panel (lazy-loaded on first open) showing function name, status badge, reason, last-ok time, and top error message per function. |
| `bluesky/web/Dockerfile` | Cloud Run container for FastAPI backend |
| `firebase.json` | Firebase Hosting config → `bluesky/web/dashboard.html` |
| `.firebaserc` | Firebase project: `sx-platform`, account: `sean@seanxavier.com` |

---

## Web Dashboard (Phase 13)

**Local dev:**
```bash
cd /Users/ko/Documents/sx-templates
bash run_local.sh
```

`run_local.sh` handles everything: checks GCP credentials (re-auths if expired), sources `.env`, starts uvicorn on port 8000, and opens the dashboard in the browser. Ctrl+C stops immediately (SIGKILL bypasses gRPC teardown delay).

**Local dev notes:**
- `file://` origin triggers CORS errors against Cloud Run — always run the local backend when editing locally
- The local backend explicitly allows `null` origin (browsers send `Origin: null` for `file://` pages) — `["null", "http://localhost:8000", "http://127.0.0.1:8000"]` in `CORSMiddleware`. Do not change to `["*"]`; wildcard + credentials is rejected by browsers for null origins.
- Google auth overlay does not show for `file://` — falls back to `DASHBOARD_SECRET` prompt; set `dash_secret` in browser console once: `localStorage.setItem('dash_secret', '<secret>')`
- Do NOT set `api_base` in localStorage for local dev — the auto-detection handles it; if set, remove with `localStorage.removeItem('api_base')`
- `GOOGLE_CLOUD_PROJECT` and `FIRESTORE_DATABASE` must be set in the server's environment — `run_local.sh` handles this automatically

**Deploy:**
```bash
# Build and push dashboard image via Cloud Build (no local Docker needed)
gcloud builds submit \
  --config /tmp/cloudbuild-dashboard.yaml \
  --project=sx-platform .
# cloudbuild-dashboard.yaml: build -f bluesky/web/Dockerfile -t gcr.io/sx-platform/sx-dashboard-api

# Deploy FastAPI to Cloud Run
gcloud run deploy sx-dashboard-api \
  --image="gcr.io/sx-platform/sx-dashboard-api" \
  --region=us-central1 --platform=managed \
  --set-secrets="DASHBOARD_SECRET=dashboard-secret:latest,ANTHROPIC_API_KEY=anthropic-api-key:latest,BRANDVOICE_CONTENT=brandvoice-content:latest" \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=sx-platform,FIRESTORE_DATABASE=sxplatformdatabase" \
  --project=sx-platform

# Allow public access (org policy blocks allUsers IAM binding — use invoker-iam-check instead)
gcloud run services update sx-dashboard-api \
  --region=us-central1 --project=sx-platform --no-invoker-iam-check

# Deploy Cloud Functions
bash deploy.sh

# Deploy frontend to Firebase Hosting
firebase deploy --only hosting --account sean@seanxavier.com

# Update Cloud Scheduler jobs (run after deploy.sh if scheduler jobs changed)
bash scheduler.sh
```

**Auth:** Firebase Google sign-in (`sean@seanxavier.com`) on the hosted dashboard. FastAPI verifies the Firebase ID token; falls back to `DASHBOARD_SECRET` for local dev/curl. Cloud Run public access uses `--no-invoker-iam-check` (org policy blocks `allUsers` IAM binding).

**Cloud Run URL:** `https://sx-dashboard-api-876891447075.us-central1.run.app`

**`API_BASE` in dashboard.html:** Auto-detects `localhost` or `file://` → `http://localhost:8000`; hosted → Cloud Run URL. Override via `localStorage.setItem('api_base', url)` if needed.

**Build order:** See `/Users/ko/.claude/plans/imperative-wishing-octopus.md` for the full 24-step sequence. Phase 1 (instrumentation) must run before the dashboard has real data — charts show empty state until `engagement_events` accumulates.

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
| 8 | ✓ | Comment engine (post scoring, queue, executor) — **currently paused** (`DAILY_COMMENT_CAP=0` in deploy.sh). Before re-enabling: review brand voice in Firestore (`_system/brand_voice`) to confirm comment tone is approved for public posts on other creators' accounts. |
| 9 | ✓ | Cloud Functions + Cloud Scheduler deploy |
| 10 | ✓ | Audit: dead code removal, conflict fixes, limit hardening, cleanup CF |
| 11 | ✓ | Adaptive DM replies: signal-gated mirroring, CTA injection after 2 exchanges; brand voice versatility fixes |
| 12 | ✓ | Initiation test fixes: reply-delay skip in Cloud Functions, cleanup-stale-docs UnboundLocalError, execute-dm-batch batch_size override, Firestore composite indexes |
| 13 | ✓ | Web dashboard — instrumentation → FastAPI backend → SPA frontend → deploy |
| 14 | ✓ | DM queue redesign — queued like/repost/comment_exchange DMs, active hours gate (7am–10pm Pacific), dual write windows (60s DM / 4-min public), bot scorer, first-reply discount, commenter proactive DM (`fan_discount_pull`), inbound DM burst window |

Full spec: `BLUESKY_ENGAGEMENT_SPEC.md`
Phase 13 plan: `/Users/ko/.claude/plans/imperative-wishing-octopus.md`

### Post-deploy design notes

**Session persistence** (`bluesky_client.py`)
`BlueskyClient.login()` stores the atproto session string in Firestore `_system/bluesky_session` after every login. On the next invocation it restores from that doc instead of calling `createSession`. The SDK auto-refreshes the access token (~2hr lifetime) using the stored refresh token (~90 days) without counting against the 300/day `createSession` quota. A full `createSession` only occurs on first run or after ~90-day refresh token expiry.

**Inbound DM polling** (`fan_pipeline.py → poll_inbound_dms`)
Active hours gate + burst window gate run at the top — returns early if outside 7am–10pm Pacific or outside the 60-min burst window (first 60 min of each 3-hour block: 12am, 3am, 6am, 9am, 12pm, 3pm, 6pm, 9pm). `poll-inbound-dms` CF runs every 3 min; the burst window limits actual processing to ~20 cycles/day.

Uses `chat.bsky.convo.listConvos` (sorted by recent activity, includes `unread_count` per convo) instead of streaming all Firestore conversations. Only conversations with `unread_count > 0` are processed. Pagination stops as soon as a full page has no unread entries — unread convos cluster at the top of the list.

At the start of each `poll_inbound_dms` cycle, before processing `listConvos`, the function queries Firestore for conversations where `has_pending_manual_reply == True` and sends each via `client.send_dm(convo_id, pending_manual_reply)`. On success, `pending_manual_reply` is cleared and the message is written to the `messages` subcollection as `role: "assistant"`. This is the delivery path for replies saved via the dashboard handoff modal.

**`human_handoff` flag behavior**
The flag is a per-reply guard, not a polling filter. When a handed-off conversation has a new fan message, it appears in `listConvos` as unread, the flag is checked, and we skip without replying — leaving the message unread in Bluesky so a human operator can see it in the app.

Handoff cases are resolved from the dashboard (Human handoff queue card → click queued item to open modal) or via CLI: `python bluesky/reply/admin.py clear-handoff @handle`. The dashboard modal supports 4 outcomes:

| Reply entered | Resume bot | Queue action | Result |
|---|---|---|---|
| ✓ | ✓ | Remove from queue | Reply queued (`pending_manual_reply`), `human_handoff=False` |
| ✓ | ✗ | Remove from queue | Reply queued, `human_handoff=False`, handle added to `paused_users` |
| ✓ | ✗ | Keep in queue | Reply saved to history, `human_handoff` stays True |
| ✗ | ✓ | Remove from queue | `human_handoff=False`, bot resumes |
| ✗ | ✗ | Remove from queue | `human_handoff=False`, handle added to `paused_users` |
| ✗ | ✗ | Keep in queue | No-op, stays in queue |

"Keep in queue" is disabled in the UI when "Resume bot" is on. "Remove from queue + don't resume" adds to `paused_users` in `state.json` so the bot skips the handle on future cycles.

**Handoff trigger threshold:** `exchange_count >= 10` (raised from 8 — conversations with CTAs now resolve earlier).

**User Activity modal** (`dashboard.html`)
Accessible via a `look up @handle…` typeahead on the Activity page filter bar and the dashboard Activity Feed card header. Typeahead queries `GET /api/handles?q=` (prefix-searches `conversations` by `fan_handle`, returns handle + user_type + stage + handoff flag). On selection, opens a modal with:
- Left panel: engagement history from `engagement_events` (type + timestamp)
- Right panel: full message thread (bot/fan/operator bubble styles)
- Send box: queues a manual reply via `POST /api/user/{handle}/dm` → sets `pending_manual_reply` + `has_pending_manual_reply: True`; bot delivers it on the next `poll_inbound_dms` cycle. Send button is disabled when no `conversations` doc exists for the handle.

The User Activity modal is distinct from the handoff modal — it works for any handle with logged activity, not just handoff queue entries.

**`send_engagement_dm` silent failure risk**
`client.send_dm()` and the Firestore `conversations.set()` are in the same `try` block but not atomic. If the DM sends but the Firestore write throws (transient error, quota), the DM is delivered but no `conversations` doc is created. Consequences: `_already_dmed()` returns false (handle is re-outreachable on next engagement), `GET /api/user/{handle}` returns 404, activity monitor may show the DM via the activity log while the modal shows "could not load details". Mitigation: separate the Firestore write into its own try/except so a write failure is logged independently without masking the send success.

**Inbound DM polling — local vs Cloud Functions**
`poll_inbound_dms()` is called in two places:
- **Local mode** (`poller.py --interval 60`): called directly in the `main()` loop after each `run_once()`.
- **Cloud Functions**: called by the dedicated `poll-inbound-dms` CF (every **3 min**). The `poll-notifications` CF calls only `run_once()` and does NOT call `poll_inbound_dms` — the separate CF handles it. This prevents double-processing.

**Rate limiter — dual write windows**
`check_read()` is tracked in-memory per process (reads are IP-based; Firestore coordination adds no value and would cost ~20K extra ops during follower graph runs).

`check_write()` / `seconds_until_next_write()` — global 4-min public write window, tracked in Firestore `_system/rate_state` (`last_write_at` field). Applies to public replies and comments.

`check_dm_write()` / `seconds_until_next_dm_write()` — 60s DM-specific write window, tracked in Firestore `_system/rate_state` (`last_dm_write_at` field). Fully independent of the 4-min public window — a burst of DMs does not delay public replies and vice versa. Shares the same hourly/daily point budget as `check_write()`.

**Write window now covers replies**
`post_reply()` in `poller.py` now goes through `check_write()` / `seconds_until_next_write()`, the same as DM sends and comment posts. All three write paths share the global 4-minute window.

**Reply posting delay — local only**
`poller.py` applies a `random.uniform(90, 600)s` human-pacing delay before posting replies, and waits for the write window before calling `check_write`. Both sleeps are gated on `not os.environ.get("K_SERVICE")` — Cloud Run sets `K_SERVICE` automatically, so the delays only fire in local mode. In Cloud Functions, if the write window isn't clear, `check_write` raises `RateLimitError` and the reply is skipped (stays unseen, retried on the next 5-min invocation).

**Engagement DM queue** (`fan_pipeline.py → execute_engagement_dm_queue`)
`poll-notifications` queues like/repost/comment_exchange DMs via `queue_dm()` (no immediate send). The dedicated `execute-engagement-dms` CF (every 5 min, 300s timeout) drains these:
1. Active hours gate — returns early if outside 7am–10pm Pacific.
2. Queries `dm_queue` for `status=="pending"` and `trigger_type in ("like","repost","comment_exchange")`, sorted by `interaction_at` DESC (most recent first), up to `batch_size=10`.
3. Per item: skips if `interaction_at` > 1hr ago (`engagement_too_old`); skips if `_already_dmed(handle)` (`already_dmed`); sleeps `random.uniform(90, 600)s` between sends (skipped before the first send per cycle); calls `check_dm_write()` (60s DM window).
4. Marks sent, writes `conversations` doc and `messages` subcollection, writes `engagement_events` doc.

Throughput: ~4 DMs per 5-min cycle (vs. 1 DM per 5-min cycle with the old immediate-send model under the 4-min public write window).

**DM engagement event writes — all paths**
All outbound DM paths write an `engagement_events` doc (`type="dm"`, `direction="outbound"`, `interaction_subtype="{trigger}_trigger"`):
- `execute_engagement_dm_queue()` — like/repost/comment_exchange sends (queued path)
- `process_dm_queue()` — follow batch sends
- `poll_inbound_dms()` — also writes an inbound event (`type="reply"`, `direction="inbound"`, `fan_intent`) when a fan reply is received and handled

**`/api/dm-effectiveness` data source**
Uses `conversations` collection as the source of truth. Groups by `trigger_context` → maps to dashboard subtype keys (`like` → `like_trigger`, `repost` → `repost_trigger`, `follow` → `follow_trigger`, `reply_dm_pull` → `reply_dm_pull`). "Responded" = `stage` in `{engaged, converted, subscriber}`; `warm` and `dm_pull_sent` mean no fan reply yet.

**Effectiveness = `discount_sent=True`** — a separate query loads all conversations where `discount_sent=True` (no time filter), then filters by `discount_sent_at` → `last_message_at` → `created_at` against the range window. This correctly captures discounts sent on conversations that were *created* before the range window. Same definition applies to the Conversion graph's Effectiveness fill.

**Conversion graph metrics** (Conversion tab, `/api/funnel`):
| Metric | Definition |
|---|---|
| DMs sent | `engagement_events` type=dm, direction=outbound — white line |
| Engagement | `conversations` where `stage in {engaged, converted, subscriber}`, bucketed by `created_at` — teal outer fill |
| Effectiveness | `conversations` where `discount_sent=True`, bucketed by `discount_sent_at` / `last_message_at` — teal inner fill |
| Posts | `engagement_events` type=post, fixed-height markers — written by `_snapshot_my_posts()` in `poll_inbound_dms` |

**DM batch (follows only)**
`execute_dm_batch` (Cloud Functions) uses `batch_size=10`. With 5–15 min stagger per send (`random.uniform(300, 900)s`) and a 3600s timeout, 10 items is the practical maximum (10 × 300s = 3000s). Only follow-triggered DMs go through this path. Items not sent remain pending and are picked up at the next 4-hour invocation.

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

**Function health monitoring** (`main.py` + `dashboard.html`)
`_FUNCTION_HEALTH_CONFIG` is a 12-function strategy table in `bluesky/web/api/main.py`. Each entry has a strategy key:
- `consecutive` — alert if ≥N consecutive errors (for functions that run every few minutes)
- `any_today` — alert if any error today (for functions that run infrequently)
- `any_occurrence` — alert on any error ever (high-severity functions)
- `warning_only` — never increments the error count, shows as amber warning

`_eval_fn_health(fn_name, config, error_docs)` evaluates one function against its strategy and returns `{"status": "ok"|"warning"|"error", "reason": str, "last_ok": iso_ts, "top_errors": [...]}`.

`_build_error_detail(fn_name, result)` formats the drilldown row for `GET /api/errors`.

`GET /api/health` — `error_count_today` is now count of alerting functions (not raw error docs). Adds `has_warnings: bool`.

`GET /api/errors` — returns per-function breakdown sorted errors → warnings → ok. Used by the health bar panel in `dashboard.html`.

`dashboard.html` health bar: `errors N` pill is clickable. Red when any functions are alerting, amber when only warnings. Clicking toggles an inline panel (lazy-loaded on first open — no extra poll cost) showing: function name | status badge | reason | last-ok time | top error message.

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
