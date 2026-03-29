# Dashboard spec — SX Platform
**For Claude Code.** Maps every UI element to its data source, query logic, and update behaviour.
Reference the approved visual mockup at `web/mockups/sx-dashboard-v7.html`.

---

## Architecture

**Backend:** FastAPI app at `web/api/main.py`. Deployed on Cloud Run; also runnable locally with `uvicorn`.
**Frontend:** Single-file SPA at `web/dashboard.html` (built from mockup). Uses `showPage()` client-side routing — no server-side routing needed.
**Auth:** `DASHBOARD_SECRET` env var, validated as `Authorization: Bearer <secret>` on every request.
**Firestore:** `GOOGLE_CLOUD_PROJECT=sx-platform`, database name from `FIRESTORE_DATABASE` env var.

---

## Pages

| Page | Icon | Key interactions |
|---|---|---|
| Dashboard | ⊞ | Overview charts, handoff queue preview, tone review preview, activity preview |
| Content performance | ▦ | Post grid, sort/filter, detail panel, shift+click funnel nav |
| Tone review | ◎ | Tabbed: Editorial review + Classification review with labeling sessions |
| Activity | ≡ | Paused interactions, full expandable feed, per-handle resume |
| Brand voice | ❧ | Section editor, tag inputs, rule lists, live preview, push to bot |
| Spend | $ | Daily chart, call type/model breakdown, raw events |
| Settings | ⚙ | Global pause toggle, caps, creator detection, discounts, danger zone |

---

## Mobile navigation

On mobile (≤680px): icon sidebar hidden; hamburger in every topbar opens a slide-in drawer (220px, z-index 31) with icons + labels. Overlay behind closes it.

---

## Time range

`range` param: `24h` (by hour) | `7d` (by day) | `30d` (by week). All Firestore timestamps normalised to UTC before bucketing.

---

## Settings persistence

Writable settings are stored in Firestore `_system/settings`. On bot startup, settings are read from this doc if present, overriding env var defaults. This is what makes `POST /api/settings` durable across restarts.

---

## Endpoints — Dashboard page

### GET `/api/health`
**Sources:** `state.json` → `bot_status`; `_system/rate_state` → `last_write_at`; `_system/activity_log` → most recent doc; `_system/settings` → `bot_status` override.

**Response:**
```json
{
  "bot_status": "running",
  "last_run_at": "2026-03-28T14:22:00Z",
  "last_run_ago_seconds": 120,
  "rate_limit_pct_used": 12,
  "write_window": "clear",
  "error_count_today": 0
}
```

Frontend polls every 30s. When `bot_status == "paused"`, the health bar dot turns amber and label reads "bot paused".

---

### GET `/api/funnel?range=24h`
Powers the Conversion tab of the Reach & conversion card.

**Sources:** `engagement_events` collection.

For each time bucket:
- `fan_replies` → count where `type == "reply"` and `direction == "inbound"`
- `dms_sent` → count where `type == "dm"` and `direction == "outbound"`
- `posts` → count where `type == "post"` and `author == MY_HANDLE`
- `dm_effectiveness_pct` → DMs that received a reply from the same handle within 48hrs / total DMs × 100

**Chart interactions (frontend):**
- Regular click → drills into DM breakdown, calls `/api/funnel/snapshot` + `/api/dm-effectiveness?period=label`
- Shift+click → navigates to Content performance page, sets `cpFilterActive=true` and `cpFilterLabel=label`

**Response:**
```json
{
  "range": "7d",
  "buckets": [
    { "label": "Mon", "timestamp": "2026-03-25T00:00:00Z", "fan_replies": 42, "dms_sent": 10, "posts": 6, "dm_effectiveness_pct": 24 }
  ]
}
```

---

### GET `/api/funnel/snapshot?period=Mon&range=7d`
Powers the DM engagement card's period badge and summary when a funnel column is clicked.

**Sources:** `engagement_events`, `state.json` (`conversation_depth`, `dm_pulls_by_root`).

Filter `engagement_events` to the selected bucket, then count:
- Stage 1 `fan_replies` → inbound reply count
- Stage 2 `nudge_sent` → outbound replies where `reply_type == "nudge"`
- Stage 3 `intent_signal` → inbound follow-up replies where `fan_intent` in (`buying_signal`, `curious`)
- Stage 4 `dm_pull` → outbound DMs triggered from reply thread

**Response:**
```json
{ "period": "Mon", "fan_replies": 42, "nudge_sent": 31, "intent_signal": 20, "dm_pull": 10 }
```

---

### GET `/api/growth?range=7d`
Powers the Audience growth tab of the Reach & conversion card.

**Sources:** `_system/follower_snapshots/daily/{date}` (subcollection), `conversations` (user_type counts), `target_accounts`.

**Requires:** `snapshot-follower-count` Cloud Scheduler job running nightly (writes `{ date, count }` to `_system/follower_snapshots/daily/{YYYY-MM-DD}`). Until this job has run, all follower trend values will be 0.

**Follower quality score:** Not yet implemented — displays `—` in the UI. Spec: ratio of classified-real accounts vs likely bots, stored as `quality_flag: "likely_bot"` on `target_accounts`. Quality bands: 0–40 poor · 41–65 fair · 66–80 good · 81–100 excellent.

**Known bug fixed:** `range` parameter name shadowed Python's built-in `range()`, causing a 500 on every call. Fixed by rewriting the delta loop without `range()`.

**Response:**
```json
{
  "range": "7d",
  "labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
  "total": [0, 0, 0, 0, 0, 0, 0],
  "daily": [0, 0, 0, 0, 0, 0, 0],
  "total_followers": 0,
  "new_today": 0,
  "weekly_gain": 0,
  "weekly_growth_pct": 0.0,
  "follower_type_breakdown": { "fan": 0, "creator": 0, "themed": 0, "studio": 0, "likely_bot": 0 },  // counts from conversations collection, not total followers — UI label: "Conversation breakdown by type"
  "discovery_this_week": { "tier_1": 0, "tier_2": 0, "tier_3": 0, "total": 0 }
}
```

Note: `quality_score` and `quality_score_label` removed from response — not yet computed.

---

### GET `/api/dm-effectiveness?range=24h&period=Mon`
Powers the DM engagement card. `period` optional.

Returns `{by_type: {like_trigger: {sent, responded}, ...}}`. The frontend always renders all 6 known DM types regardless of whether `by_type` contains them — missing types default to `{sent:0, responded:0}`. Do not change this behaviour; zero rows are intentional so the full type list is always visible.

**Known DM subtypes:**
| `interaction_subtype` | UI label |
|---|---|
| `like_trigger` | Like outreach |
| `repost_trigger` | Repost outreach |
| `follow_trigger` | Follow outreach |
| `reply_dm_pull` | Reply-thread pull |
| `collab_outreach` | Collab outreach |
| `convo_cta` | Convo CTA (planned, not yet written) |

**Note:** 769 historical DM events were missing `interaction_subtype`. Backfilled via `scripts/backfill_dm_subtypes.py` using `conversations.trigger_context`. Script retained for future use.

---

### GET `/api/heatmap?mode=replies`
`mode`: `replies` | `dms`. Always 24h × 7d.

Frontend calls `loadHeatmap(mode)` which fetches from this endpoint and calls `buildHeatmap()` with real data. The previous `genHmData()` fake data generator has been removed. Heatmap initialises with all zeros until the API responds.

---

### GET `/api/handoff`
Powers the handoff queue card on dashboard (preview, 3 items max) and the full paused section on Activity page.

Urgency: `high` → waiting > 1hr + payment keywords; `med` → waiting > 2hrs; `low` → everything else.

**Response:**
```json
{
  "count": 3,
  "items": [
    { "handle": "@jkfan99", "initials": "JK", "preview": "i already paid...", "reason": "payment issue", "urgency": "high", "waiting_since": "...", "waiting_minutes": 47, "convo_id": "abc123" }
  ]
}
```

---

### GET `/api/tone-review?vehicle=&interaction=`
Powers both the dashboard tone review card (preview, 3 items) and the full Tone review page.

**Optional filter params:**
- `vehicle`: `reply` | `dm` | `comment` — filters by interaction vehicle
- `interaction`: `nudge` | `dm_pull` | `subscriber_warmth` | `like_outreach` | `repost_outreach` | `convo_reply` | `tier_1` | `peer_comment`
- `surface`: `edge_case` | `sample` — omit for all
- `limit`: default 10 for full page, 3 for dashboard preview

**Surfacing logic** (nightly, cached in `_system/tone_review_queue`):
- Edge cases: first `reply_type` + `interaction_subtype` combo; low-confidence classification; handoff within 2 exchanges; subscriber guard with no prior `conversations` doc
- Random sample: 1 per vehicle × interaction type
- Counts by vehicle/interaction returned in response for sidebar filter badges

**Response:**
```json
{
  "total": 8,
  "by_vehicle": {
    "reply": { "all": 5, "nudge": 2, "dm_pull": 2, "subscriber_warmth": 1 },
    "dm": { "all": 3, "like_outreach": 1, "repost_outreach": 1, "convo_reply": 1 },
    "comment": { "all": 2, "tier_1": 1, "peer_comment": 1 }
  },
  "session_history": { "last_session_days_ago": 2, "approved": 14, "flagged": 3, "bv_updates": 2 },
  "items": [
    {
      "id": "evt_abc123",
      "vehicle": "dm",
      "interaction_type": "upsell",
      "surface_reason": "edge_case",
      "handle": "@britneyf",
      "fan_message": "Reposted: New content dropping Friday",
      "bot_reply": "Hey! Loved your post...",
      "classification": { "post_type": "promotional", "fan_intent": "curious", "mirror_tier": "low", "interaction": "repost_trigger" },
      "created_at": "2026-03-28T11:00:00Z"
    }
  ]
}
```

---

### POST `/api/tone-review/:id/feedback`
Records approve/flag. **Body:** `{ "action": "approve" | "flag" }`

Approved → write to `_system/tone_review_feedback`; store as few-shot example.
Flagged → write to `_system/tone_review_feedback`; surface in next modal open.

---

### GET `/api/activity?range=24h&type=all`
Powers the Activity page full feed and the dashboard activity preview (limit 5).

**Params:**
- `type`: `all` | `reply` | `dm` | `comment` | `flag` | `paused` | `discount`
- `range`: `24h` | `7d` | `30d`
- `limit`: default 50 for full page, 5 for preview
- `handle`: optional — filter to a specific handle

**Sources:** `engagement_events` + `_system/activity_log` + `conversations` (for paused type).

Each item includes a `detail` field with full classification context for the expandable row.

**Response:**
```json
{
  "items": [
    {
      "id": "evt_xyz",
      "type": "dm",
      "subtype": "dm_pull",
      "description": "DM pull sent to @britneyf",
      "handle": "@britneyf",
      "created_at": "2026-03-28T14:20:00Z",
      "ago": "2 min ago",
      "detail": "Fan intent: buying_signal · Post type: promotional · Mirror tier: low · Discount: none"
    }
  ]
}
```

---

### GET `/api/activity/paused`
Powers the paused interactions section on the Activity page.

**Source:** `conversations` where `human_handoff == true` OR `paused_users` in `state.json`.

**Response:**
```json
{
  "count": 3,
  "items": [
    {
      "handle": "@jkfan99",
      "initials": "JK",
      "reason": "payment issue",
      "paused_since": "2026-03-28T13:35:00Z",
      "paused_minutes": 47,
      "pause_type": "handoff"
    }
  ]
}
```

`pause_type`: `handoff` (auto-triggered by handoff detection) | `manual` (set via admin.py).

---

### POST `/api/activity/resume/:handle`
Resumes automated replies for a specific handle.

**Logic:**
1. Clear `human_handoff` flag on `conversations` doc for this handle (sets to `false`)
2. If handle is in `paused_users` in `state.json`, remove it
3. Write to `_system/activity_log` that the resume was manually triggered

**Response:** `{ "handle": "@jkfan99", "resumed_at": "...", "success": true }`

Frontend: on success, remove the handle from the paused section and show a toast.

---

### GET `/api/caps`
Powers the three daily cap cards. Unchanged.

---

### GET `/api/insights?range=24h`
Python-computed insights bar — no LLM involved. Counts are pulled directly from `engagement_events` and `_system/settings`. Returns `{html, cached, cached_at}` where `html` is pre-formatted with `<strong>` tags for bolding. Cached in `_system/insights_cache`, 1-hour TTL.

**Metrics shown (last 24h):**
- Buying signals (`fan_intent == "buying_signal" | "curious"`)
- Subscriber guard fires (`reply_type == "subscriber_warmth"`)
- Discounts sent (`interaction_subtype == "discount_sent"`) — shown as % of daily cap
- Handoffs (`interaction_subtype == "handoff"`)
- New followers (inbound follow events)

Each metric shows a "none today" fallback when count is zero. Do not switch back to LLM generation — previous approach hallucinated stats.

---

## Endpoints — Content performance page

### GET `/api/posts?range=7d&sort=recent&type=all&period=`
Powers the post grid on the content performance page.

**Params:**
- `sort`: `recent` | `dm_pulls` | `replies`
- `type`: `all` | `promo` | `personal`
- `period`: optional — filters to posts from a specific time bucket (set when shift+clicking the funnel chart)

**Sources:** `engagement_events` (post events + reply/DM aggregates), Bluesky API (post text + embed images via `bluesky_client.py`, cached in `_system/post_cache` keyed by URI).

Post type classification uses `classify_post_type()` from `reply_generator.py` — cache result on the `engagement_events` doc as `post_type_classification`. If `image_url` is null (no embed), frontend shows a placeholder emoji based on post type.

**Response:**
```json
{
  "range": "7d",
  "sort": "recent",
  "type": "all",
  "period_filter": null,
  "posts": [
    {
      "uri": "at://did:plc:xxx/app.bsky.feed.post/abc",
      "text": "New content dropping Friday...",
      "image_url": "https://cdn.bsky.app/img/...",
      "post_type": "promotional",
      "created_at": "2026-03-28T11:02:00Z",
      "created_label": "Fri 11:02am",
      "fan_replies": 22,
      "dm_pulls": 8,
      "dm_pull_rate_pct": 36,
      "comments_posted": 3
    }
  ]
}
```

### GET `/api/posts/:uri`
Powers the post detail panel. `:uri` is URL-encoded.

Returns all fields from `/api/posts` for this post, plus:
- `nudge_intent_rate_pct` → % of nudged fans who replied with buying signal or curious
- `engagement_peak_offset_hrs` → hours between post time and peak reply hour
- `hourly_replies` → array[6] of reply counts for hours 0–5 after posting

**Response:**
```json
{
  "uri": "at://...",
  "text": "New content dropping Friday...",
  "image_url": "https://cdn.bsky.app/img/...",
  "post_type": "promotional",
  "created_at": "2026-03-28T11:02:00Z",
  "created_label": "Fri 11:02am",
  "fan_replies": 22,
  "dm_pulls": 8,
  "dm_pull_rate_pct": 36,
  "nudge_intent_rate_pct": 68,
  "comments_posted": 3,
  "engagement_peak_offset_hrs": 1.2,
  "hourly_replies": [2, 4, 8, 5, 2, 1]
}
```

### GET `/api/user/:handle`
Powers the case review modal from the handoff queue.

**Sources:** `conversations`, `messages` subcollection, `engagement_events` filtered by handle, `dm_state.json`.

**Response:**
```json
{
  "handle": "@jkfan99",
  "user_type": "fan",
  "follower_count": 142,
  "classified_at": "2026-03-20T10:00:00Z",
  "human_handoff": true,
  "conversation": { "exchange_count": 4, "stage": "cta_sent", "last_message_at": "2026-03-28T13:35:00Z" },
  "messages": [ { "sender": "fan", "text": "...", "created_at": "..." } ],
  "engagement_history": [ { "type": "like", "post_uri": "...", "created_at": "..." } ]
}
```

---

## Endpoints — Tone review page (Classification tab)

### GET `/api/classifier/:type/stats`
Returns current accuracy and trend for one classifier. `:type`: `intent` | `posttype` | `subguard` | `handoff`.

**Source:** `_system/classifier_stats` doc, updated after each labeling session.

**Response:**
```json
{
  "type": "intent",
  "label": "Fan intent classifier",
  "accuracy_pct": 91,
  "trend_pct": 3,
  "trend_direction": "up",
  "labeled_this_month": 142,
  "corrections_this_month": 12,
  "health": "healthy",
  "pending_review_count": 0,
  "classifies": "buying_signal · curious · casual"
}
```

`health`: `healthy` (accuracy ≥ 85%, trend flat/up) | `needs_attention` (accuracy < 85% OR trend down > 3%) | `critical` (accuracy < 70%).

---

### GET `/api/classifier/:type/session?limit=10`
Returns N items for a labeling session.

**Logic:** Pull recent `engagement_events` docs where the classifier fired. For `intent`: docs where `fan_intent` was set. For `posttype`: docs where `post_type_classification` was set. Shuffle and return `limit` items.

**Response:**
```json
{
  "type": "intent",
  "items": [
    {
      "id": "evt_abc",
      "context": "Fan reply · promo post · first interaction",
      "text": "I need this in my life rn",
      "current_classification": "buying_signal",
      "question": "Is this a buying signal?"
    }
  ]
}
```

---

### POST `/api/classifier/:type/label`
Records one labeling answer.

**Body:** `{ "item_id": "evt_abc", "label": true | false | null }`

- `true` → classification was correct
- `false` → classification was wrong (correction)
- `null` → skipped

**Logic:**
1. Write to `_system/classifier_labels` collection
2. If `label == false`: flag item for retraining review; increment correction count
3. After session completes (all items answered): recalculate accuracy from recent labels, update `_system/classifier_stats`

**Response:** `{ "item_id": "evt_abc", "recorded": true, "session_complete": false }`

---

## Endpoints — Settings page

### GET `/api/settings`
Returns all configurable settings. Sources: `_system/settings` Firestore doc + env vars (env vars are the fallback defaults).

**Response:**
```json
{
  "bot": {
    "status": "running",
    "poll_interval_seconds": 60,
    "max_conversation_depth": 3,
    "reply_delay_mode": "90-600s"
  },
  "caps": {
    "max_discounts_per_day": 5,
    "max_comments_per_day": 50,
    "max_dm_outreach_per_day": 50,
    "monthly_spend_cap_usd": null
  },
  "creator_detection": {
    "mutual_follow": true,
    "bio_keywords": false,
    "follower_count": false,
    "follower_threshold": 500,
    "collab_dm_threshold": 20000
  },
  "studio_handles": [],
  "themed_handles": [],
  "discounts": {
    "fan_discount_code": "",
    "fan_discount_url_reply": "",
    "fan_discount_url_like": "",
    "fan_discount_url_repost": ""
  },
  "notifications": {
    "handoff_alerts": true,
    "rate_limit_alerts": true,
    "discount_cap_alerts": true,
    "spend_cap_alerts": false
  }
}
```

---

### POST `/api/settings`
Writes updated settings to `_system/settings` and updates running process state.

**Body:** Partial or full settings object (only changed fields required).

**Special handling:**
- `bot.status` change → also update `state.json` `bot_status` field + write to `_system/activity_log`
- Cap changes → immediately effective for next bot invocation (bot reads from Firestore)
- Creator detection changes → immediately effective for next classification

**Response:** `{ "updated_at": "...", "fields_changed": ["bot.status", "caps.max_discounts_per_day"] }`

**Danger zone actions** are sent as special POST bodies:
```json
{ "action": "clear_dedup_state" }
{ "action": "reset_user_classifications" }
{ "action": "clear_dm_queue" }
{ "action": "clear_comment_queue" }
```

---

## Instrumentation note

Fields to add to `engagement_events` writes in `reply_generator.py`, `dm_generator.py`, `comment_engine.py`:

| Field | Type | Source |
|---|---|---|
| `reply_type` | string | `nudge`, `dm_pull`, `subscriber_warmth`, etc. |
| `interaction_subtype` | string | `repost_trigger`, `buying_signal`, etc. |
| `fan_intent` | string | output of `classify_fan_intent()` |
| `mirror_tier` | string | `low` \| `medium` \| `high` |
| `post_type_classification` | string | output of `classify_post_type()` |
| `token_usage_input` | int | `response.usage.input_tokens` |
| `token_usage_output` | int | `response.usage.output_tokens` |
| `model` | string | model string used |

New Cloud Scheduler job: `snapshot-follower-count` — nightly, writes `{ date, count }` to `_system/follower_snapshots`.

New Firestore collections/docs:
- `_system/settings` — writable bot settings
- `_system/classifier_stats` — per-classifier accuracy history
- `_system/classifier_labels` — individual labeling session answers
- `_system/tone_review_queue` — cached surfaced items (refreshed nightly)
- `_system/tone_review_feedback` — approve/flag history

---

## Frontend wiring notes

All wiring guidance is in `CLAUDE_CODE_HANDOFF.md` steps 20–24.

Key frontend wiring notes for new pages:
- `toggleGlobalPause()` → `POST /api/settings` with `{ bot: { status } }` → update health bar dot and label in same call
- `resumeHandle()` → `POST /api/activity/resume/:handle` → remove item from paused section, show toast
- `startLabelSession(type)` → `GET /api/classifier/:type/session` → `renderLabelItems()`
- `labelAnswer(type, val)` → `POST /api/classifier/:type/label` → advance session, update progress bar
- Settings form → on any change → debounce 800ms → `POST /api/settings`
- Brand voice `bvPush()` → `POST /api/brand-voice`
