# Query bar & spend spec — SX Platform
**For Claude Code.** Query bar (all 7 pages) and financial spend tracking.
Reference the approved visual mockup at `web/mockups/sx-dashboard-v7.html`.

---

# Feature 1 — Natural language query bar

## UI placement

The query bar appears on **all 7 pages** as a persistent full-width strip directly below the topbar, always visible (sticky, z-index 8). Each page has its own query input and result div with unique IDs to prevent interference.

**Page-specific input/result IDs:**
- Dashboard: `qDash` / `qrDash` / `qiDash`
- Content: `qContent` / `qrContent` / `qiContent`
- Tone review: `qTone` / `qrTone` / `qiTone`
- Activity: `qActivity` / `qrActivity` / `qiActivity`
- Brand voice: `qBV` / `qrBV` / `qiBV`
- Spend: `qSpend` / `qrSpend` / `qiSpend`
- Settings: `qSettings` / `qrSettings` / `qiSettings`

The result panel sits below the query bar, hidden by default, shown with `.show` class on result return. "✕ close" button dismisses it.

---

## API endpoint

### POST `/api/query`

**Body:**
```json
{
  "question": "how many DMs sent today",
  "context_range": "24h",
  "context_page": "dashboard"
}
```

`context_page`: `dashboard` | `content` | `tone` | `activity` | `brand_voice` | `spend` | `settings`

**Backend logic — two-step Claude-mediated Firestore query:**

**Step 1 — Schema context prompt**

Include `context_page` to bias collection selection. Full schema description:

```
You are a data query assistant for the SX Platform bot system.
Available Firestore collections:

engagement_events: { type, handle, post_uri, direction, created_at, reply_type, fan_intent, link_url, post_type_classification, interaction_subtype, mirror_tier, model, token_usage_input, token_usage_output }
conversations: { handle, user_type, exchange_count, human_handoff, stage, last_message_at }
messages: { sender, text, created_at } [subcollection of conversations]
dm_queue: { handle, status, dm_type, created_at, sent_at }
comment_queue: { target_handle, post_uri, status, queued_at, posted_at }
api_cost_events: { provider, model, call_type, input_tokens, output_tokens, cost_usd, created_at }
target_accounts: { handle, tier, domain, quality_flag, created_at }
_system/activity_log: { function_name, timestamp, items_processed, errors }
_system/brand_voice: { version, pushed_at, identity, voice, lexicon, structural, content_rules, platform_rules, archetypes }
_system/follower_snapshots: { date, count }
_system/settings: { bot, caps, creator_detection, discounts, notifications }
_system/classifier_stats: { intent, posttype, subguard, handoff } (each: accuracy_pct, trend_pct, labeled_this_month, corrections_this_month)

Today: {TODAY}. Range context: {RANGE}. Current page: {PAGE}.

Output JSON only:
{
  "collection": "collection_name",
  "filters": [{ "field": "...", "op": "==|>=|<=|array-contains|in", "value": "..." }],
  "order_by": "field",
  "order_dir": "asc|desc",
  "limit": 50,
  "answer_fields": ["field1", "field2"],
  "needs_table": true | false,
  "summary_instruction": "plain English instruction for summarising results"
}
```

**Special case — brand_voice page:** Questions about banned words, rules, persona, or archetypes should be answered directly from the `_system/brand_voice` doc without a full Firestore query. Detect these by checking if `context_page == "brand_voice"` and the question contains vocabulary-related keywords. Return prose directly, skip Step 2.

**Special case — settings page:** Questions about current config values (caps, thresholds, URLs) should read from `_system/settings`. Return prose directly.

**Step 2 — Execute + summarise:** Parse Claude JSON → execute Firestore query → send results + `summary_instruction` back to Claude → 1–3 sentence plain English answer + optional table.

**Step 3 — Return:**
```json
{
  "question": "how many DMs sent today",
  "prose": "67 DMs sent today across all types...",
  "table": { "heads": ["type", "count"], "rows": [["like_dm", "42"], ["follow_dm", "25"]] },
  "has_table": true,
  "query_took_ms": 840
}
```

`table` is `null` when `needs_table` is false or there are no rows. Frontend renders via `table.heads` / `table.rows`.

**Error handling:**
- Unparseable JSON → `{ "error": "couldn't interpret that — try rephrasing" }`
- Firestore failure → `{ "error": "query failed: <reason>" }`
- Empty results → prose says "no results found"

**Cost:** `claude-haiku-4-5-20251001` for both steps. Cache keyed on `(question, range, page, minute_bucket)` with 5-minute TTL.

---

## Example questions by page

**Dashboard:**
- "how many DMs did I send today"
- "which fans have been waiting in handoff queue the longest"
- "did the subscriber guard fire today and for who"
- "which DM type had the highest engagement this week"
- "how many and which posts in the last hour sent a link to SeanXavier.com"
- "how many discounts were sent this month"
- "is the bot currently paused"

**Content:**
- "which post drove the most DMs this week"
- "what's my average DM pull rate on promotional posts"
- "what time of day do my posts get the most engagement"
- "show me posts from Thursday"

**Tone review:**
- "how many items are pending editorial review"
- "which classifier has the lowest accuracy right now"
- "how many corrections were made to fan intent this month"
- "when was the last labeling session"

**Activity:**
- "show me all paused handles"
- "how long has @jkfan99 been waiting"
- "how many handoffs were triggered this week"
- "show me all discount sends in the last 24 hours"

**Brand voice:**
- "what are my banned words"
- "show me the DM pull rules"
- "what archetypes are defined"
- "when was brand voice last updated"
- "what's my reply length target for nudges"

**Spend:**
- "what drove the spike on Tuesday"
- "which call type costs the most"
- "how much did I spend on DM generation this week"
- "am I on pace to hit my monthly budget"

**Settings:**
- "what are my current daily caps"
- "is creator detection enabled"
- "what discount URLs are configured"
- "what's the current poll interval"

---

# Feature 2 — Financial spend tracking

## Overview

Two surfaces:
1. **Spend summary card** on the dashboard — fourth card in the bottom row. Weekly total, trend, provider breakdown, monthly progress bar. Clicking navigates to Spend page.
2. **Spend page** — full breakdown by day, call type, model. Accessible from `$` sidebar icon.

---

## Cost instrumentation

### Anthropic API — `api_cost_events` writes

Add after every `anthropic.messages.create()` call in:
- `reply_generator.py`
- `dm_generator.py`
- `fan_pipeline.py`
- `comment_engine.py`
- `web/api/main.py` (query bar, insights, brand voice preview, classifier session generation)

```python
firestore_client.collection('api_cost_events').add({
    'provider': 'anthropic',
    'model': response.model,
    'input_tokens': response.usage.input_tokens,
    'output_tokens': response.usage.output_tokens,
    'cost_usd': calculate_anthropic_cost(response.model, response.usage),
    'call_type': call_type,
    'created_at': firestore.SERVER_TIMESTAMP,
})
```

**`call_type` values:**
- `reply_generation` — nudge/dm_pull/peer replies via `reply_generator.py`
- `dm_generation` — outreach DMs via `dm_generator.py`
- `intent_classification` — `classify_fan_intent()` calls
- `comment_generation` — comment engine posts
- `query_bar` — `POST /api/query` calls
- `insights` — `GET /api/insights` calls
- `brand_voice_preview` — `POST /api/brand-voice/preview` calls
- `classifier_session` — labeling session item generation

Centralise in `bluesky/shared/cost_calculator.py`:

```python
ANTHROPIC_PRICING = {
    'claude-sonnet-4-6': { 'input': 3.00, 'output': 15.00 },
    'claude-haiku-4-5-20251001': { 'input': 0.80, 'output': 4.00 },
    'claude-opus-4-6': { 'input': 15.00, 'output': 75.00 },
}

def calculate_anthropic_cost(model, usage):
    pricing = ANTHROPIC_PRICING.get(model, ANTHROPIC_PRICING['claude-sonnet-4-6'])
    return round((usage.input_tokens / 1_000_000) * pricing['input'] +
                 (usage.output_tokens / 1_000_000) * pricing['output'], 6)
```

### GCP costs (estimated from activity metrics)

**Cloud Functions** — estimate from `_system/activity_log` invocation counts:
```python
GCP_CF_PRICING = { 'invocation': 0.0000004, 'gb_second': 0.0000025, 'avg_duration_seconds': 8, 'memory_gb': 0.25 }
```

**Firestore** — estimate from collection sizes:
```python
GCP_FIRESTORE_PRICING = { 'read': 0.00000006, 'write': 0.00000018, 'delete': 0.00000002 }
```

---

## Firestore collection

**`api_cost_events`** — one doc per API call. Cleanup: delete docs older than 90 days (add to `cleanup-stale-docs` CF).

---

## API endpoints

### GET `/api/spend/summary`
Powers the dashboard spend summary card.

**Response:**
```json
{
  "this_week_usd": 4.82,
  "last_week_usd": 3.91,
  "trend_pct": 23,
  "trend_direction": "up",
  "this_month_usd": 14.20,
  "monthly_cap_usd": null,
  "breakdown": { "anthropic": 3.94, "gcp_functions": 0.61, "gcp_firestore": 0.22, "gcp_other": 0.05 },
  "top_cost_driver": "reply_generation",
  "top_cost_driver_usd": 2.10
}
```

`monthly_cap_usd` from `_system/settings.caps.monthly_spend_cap_usd`. Frontend: amber at 80%, red at 100%.

---

### GET `/api/spend?range=7d`
Powers the full Spend page. `range`: `7d` | `30d`.

**Response:**
```json
{
  "range": "7d",
  "total_usd": 4.82,
  "buckets": [
    {
      "label": "Mon",
      "date": "2026-03-25",
      "anthropic_usd": 0.62,
      "gcp_usd": 0.11,
      "total_usd": 0.73,
      "call_breakdown": {
        "reply_generation": 0.28,
        "dm_generation": 0.18,
        "intent_classification": 0.09,
        "comment_generation": 0.05,
        "query_bar": 0.02,
        "insights": 0.01,
        "brand_voice_preview": 0.01,
        "classifier_session": 0.01
      }
    }
  ],
  "by_model": {
    "claude-haiku-4-5-20251001": { "calls": 842, "cost_usd": 1.20 },
    "claude-sonnet-4-6": { "calls": 124, "cost_usd": 2.74 }
  },
  "by_call_type": {
    "reply_generation": { "calls": 312, "cost_usd": 2.10 },
    "dm_generation": { "calls": 203, "cost_usd": 0.98 },
    "intent_classification": { "calls": 280, "cost_usd": 0.42 },
    "comment_generation": { "calls": 89, "cost_usd": 0.28 },
    "query_bar": { "calls": 24, "cost_usd": 0.04 },
    "insights": { "calls": 24, "cost_usd": 0.03 },
    "brand_voice_preview": { "calls": 8, "cost_usd": 0.01 },
    "classifier_session": { "calls": 12, "cost_usd": 0.01 }
  },
  "raw_events": [
    { "time": "2:22pm", "call_type": "reply_generation", "model": "claude-haiku-4-5-20251001", "input_tokens": 1240, "output_tokens": 380, "cost_usd": 0.0002 }
  ]
}
```

`raw_events`: 20 most recent events.

---

## Spend page UI

**Layout (as in mockup):**
1. Topbar with 7d / 30d toggle + query bar
2. Four stat cards: this week, Anthropic, GCP, top driver
3. Stacked bar chart: daily bars split Anthropic / GCP Functions / GCP Firestore
4. Two-column breakdown: by call type (left) and by model (right) — horizontal bars
5. Raw events table

**Color coding:**
- Anthropic → `var(--purple)`
- GCP Functions → `var(--teal)`
- GCP Firestore → `var(--blue)`
- Other → `var(--text3)`

---

## Environment variables

```
DASHBOARD_SECRET=           # Bearer token for all dashboard API endpoints
MONTHLY_SPEND_CAP_USD=      # Optional budget cap; amber at 80%, red at 100%
```
