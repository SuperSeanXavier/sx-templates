# Dependency Map

Generated from static analysis of all 35 Python source files.
**Keep this file up to date after every cross-module change** (see Editing Rules in CLAUDE.md).

---

## bluesky/shared/firestore_client.py

### Imports
- stdlib: `os`
- third-party: `google.cloud.firestore.Client`

### Exposed API
- `db` — module-level `firestore.Client` singleton (lazy-initialized via `_get_client()`)

### Firestore Collections
- None (provides the client; does not access collections directly)

### Environment Variables
- `GOOGLE_CLOUD_PROJECT` — GCP project ID (default: `"sx-platform"`)
- `FIRESTORE_DATABASE` — Firestore database name (default: `"sxplatformdatabase"`)

### Called By
- `bluesky/shared/cost_calculator.py` — imports `db`
- `bluesky/shared/activity_logger.py` — imports `db`
- `bluesky/shared/rate_limiter.py` — imports `db`
- `bluesky/shared/bluesky_client.py` — imports `db`
- `bluesky/reply/reply_generator.py` — imports `db`
- `bluesky/reply/dm_generator.py` — imports `db`
- `bluesky/reply/poller.py` — imports `db`
- `bluesky/reply/admin.py` — imports `db`
- `bluesky/engagement/fan_pipeline.py` — imports `db`
- `bluesky/engagement/handoff.py` — imports `db`
- `bluesky/engagement/discovery.py` — imports `db`
- `bluesky/engagement/comment_engine.py` — imports `db`
- `bluesky/web/api/main.py` — imports `db`

### Calls
- External: `google.cloud.firestore.Client(project=..., database=...)`

---

## bluesky/shared/cost_calculator.py

### Imports
- stdlib: (none)
- third-party: `google.cloud.firestore._firestore` (type hint only)
- internal: (none — receives `db` as parameter)

### Exposed API
- `calculate_anthropic_cost(model: str, usage) -> float` — returns cost in USD from model pricing table
- `write_cost_event(db, model: str, usage, call_type: str) -> None` — writes to `api_cost_events`; silently no-ops on error

### Firestore Collections
- WRITE: `api_cost_events` — fields: `provider`, `model`, `input_tokens`, `output_tokens`, `cost_usd`, `call_type`, `created_at`

### Environment Variables
- None

### Called By
- `bluesky/reply/reply_generator.py` — calls `write_cost_event(db, model, usage, call_type)`
- `bluesky/reply/dm_generator.py` — calls `write_cost_event(db, model, usage, call_type)`
- `bluesky/engagement/handoff.py` — calls `write_cost_event(db, model, usage, call_type)`
- `bluesky/engagement/comment_engine.py` — calls `write_cost_event(db, model, usage, call_type)`

### Calls
- (pure computation + Firestore write via injected `db`)

---

## bluesky/shared/activity_logger.py

### Imports
- stdlib: `datetime`, `timedelta`, `timezone`
- internal: `bluesky.shared.firestore_client.db`

### Exposed API
- `log_run(function_name: str, metrics: dict, status: str = "ok", error_msg: str = None, duration_s: float = None) -> None`
- `get_runs(function_name=None, period="today", since=None, until=None, limit=500) -> list`

### Firestore Collections
- READ/WRITE: `function_runs` — fields: `function`, `run_at`, `date`, `status`, `error_msg`, `duration_s`, `metrics`
  - Compound index required: `function ASC, run_at ASC`

### Environment Variables
- None

### Called By
- `functions/main.py` — via `_log(fn_name, metrics, start, error)` wrapper at end of every Cloud Function handler

### Calls
- `bluesky.shared.firestore_client.db` — Firestore collection `function_runs`

---

## bluesky/shared/rate_limiter.py

### Imports
- stdlib: `time`, `datetime`, `timezone`, `zoneinfo.ZoneInfo`
- internal: `bluesky.shared.firestore_client.db`

### Exposed API
- `RateLimitError(Exception)` — raised when a write is attempted outside the window
- `is_active_hours() -> bool` — returns True if current Pacific time is between 7am–10pm
- `check_read() -> None` — enforces read ceiling (2,400/5min); raises `RateLimitError`
- `check_write(op_type="create") -> None` — enforces 4-min global write window + hourly/daily budgets; raises `RateLimitError`
- `seconds_until_next_write() -> float` — seconds until 4-min window clears
- `check_dm_write() -> None` — enforces 60s DM-specific window; raises `RateLimitError`
- `seconds_until_next_dm_write() -> float` — seconds until 60s DM window clears

### Module-level Constants
- `WRITE_COSTS = {"create": 3, "update": 2, "delete": 1}`
- `READ_CEILING_PER_5MIN = 2400`
- `WRITE_CEILING_PER_HOUR = 4000`
- `WRITE_CEILING_PER_DAY = 28000`
- `WRITE_WINDOW_SECONDS = 240` (4 min)
- `DM_WRITE_WINDOW_SECONDS = 60`
- `_ACTIVE_TZ = ZoneInfo("America/Los_Angeles")`
- `_ACTIVE_START = 7`, `_ACTIVE_END = 22`

### Firestore Collections
- READ/WRITE: `_system/rate_state` — fields: `last_write_at`, `last_dm_write_at`, `hourly_points`, `hourly_reads`, `daily_points`, `window_start`, `read_window_start`

### Environment Variables
- None

### Called By
- `bluesky/reply/poller.py` — `check_write()`, `seconds_until_next_write()`, `is_active_hours()`
- `bluesky/engagement/fan_pipeline.py` — `check_dm_write()`, `seconds_until_next_dm_write()`, `is_active_hours()`
- `bluesky/engagement/comment_engine.py` — `check_read()`, `check_write()`, `seconds_until_next_write()`
- `bluesky/engagement/discovery.py` — `check_read()`

### Calls
- `bluesky.shared.firestore_client.db` — `_system/rate_state`

---

## bluesky/shared/bluesky_client.py

### Imports
- stdlib: `os`, `datetime`, `timezone`
- third-party: `atproto.Client`, `atproto.models`
- internal: `bluesky.shared.firestore_client.db`

### Exposed API
Class `BlueskyClient`:
- `__init__(self)` — reads `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` from env; initializes `atproto.Client`
- `login(self)` — restores session from Firestore `_system/bluesky_session`; falls back to `createSession`; persists session
- `get_reply_notifications(self, max_results=200) -> list` — paginate reply notifications
- `get_engagement_notifications(self, since=None, max_results=200) -> list` — paginate like/repost/follow notifications
- `get_post(self, uri) -> PostView` — fetch post by AT URI
- `get_profile(self, handle) -> ProfileView` — fetch profile including `viewer.following`
- `post_reply(self, text, parent_uri, parent_cid, root_uri, root_cid) -> None` — post public reply
- `get_dm_convo_status(self, handle) -> dict` — `{convo_id, last_sender, consecutive_mine, last_their_message}`
- `send_dm(self, convo_id, text) -> None` — send DM via `chat.bsky.convo.sendMessage`
- `list_convos(self, limit=100, cursor=None) -> (list, cursor)` — list DM conversations
- `get_author_feed(self, actor, limit=10, cursor=None) -> (list, cursor)` — get author's posts
- `get_followers_page(self, actor, limit=100, cursor=None) -> (list, cursor)` — paginate followers
- `get_follows_page(self, actor, limit=100, cursor=None) -> (list, cursor)` — paginate follows
- `search_starter_packs(self, query, limit=25) -> list` — search starter packs
- `get_starter_pack(self, uri) -> StarterPackView` — fetch single starter pack
- `get_list_members_page(self, list_uri, limit=100, cursor=None) -> (list, cursor)` — get list members

### Firestore Collections
- READ: `_system/bluesky_session` — field: `session` (atproto session string)
- WRITE: `_system/bluesky_session` — field: `session`, `updated_at`

### Environment Variables
- `BLUESKY_HANDLE` — Bluesky handle (e.g. `seanxavier.bsky.social`)
- `BLUESKY_APP_PASSWORD` — Bluesky app password

### Called By
- `bluesky/reply/poller.py` — `BlueskyClient()`, `.login()`, `.get_reply_notifications()`, `.get_engagement_notifications()`, `.get_profile()`, `.get_post()`, `.post_reply()`
- `bluesky/reply/scan_and_test.py` — `BlueskyClient()`, `.login()`, `.get_author_feed()`, `.get_profile()`
- `bluesky/engagement/fan_pipeline.py` — `.get_dm_convo_status()`, `.send_dm()`, `.list_convos()`
- `bluesky/engagement/discovery.py` — `.get_followers_page()`, `.get_follows_page()`, `.get_starter_pack()`, `.search_starter_packs()`, `.get_list_members_page()`
- `bluesky/engagement/comment_engine.py` — `.get_author_feed()`, `.post_reply()`
- `functions/main.py` — instantiates via `_client()` helper

### Calls
- External: `atproto.Client` (Bluesky API)
- `bluesky.shared.firestore_client.db` — session persistence

---

## bluesky/reply/state_manager.py

### Imports
- stdlib: `copy`, `json`, `os`, `pathlib.Path`

### Exposed API
Module-level constants:
- `MAX_CONVERSATION_DEPTH: int` — `int(os.environ.get("MAX_CONVERSATION_DEPTH", "3"))`

Class `StateManager`:
- `__init__(self, path=None)` — path defaults to `STATE_PATH` env or `bluesky/reply/state.json`; `_load()` uses `copy.deepcopy(_EMPTY)` when file absent (prevents shared mutable state across instances)
- `has_replied(self, post_uri) -> bool`
- `mark_replied(self, post_uri) -> None`
- `is_my_reply(self, uri) -> bool`
- `add_my_reply(self, uri) -> None`
- `get_dm_pulls(self, root_uri) -> list[str]`
- `add_dm_pull(self, root_uri, text) -> None`
- `get_depth(self, root_uri) -> int`
- `increment_depth(self, root_uri) -> None`
- `at_max_depth(self, root_uri) -> bool` — `depth >= MAX_CONVERSATION_DEPTH`
- `is_blocked(self, handle) -> bool`
- `block_user(self, handle) -> None`
- `unblock_user(self, handle) -> None`
- `is_paused_user(self, handle) -> bool`
- `pause_user(self, handle) -> None`
- `get_status(self) -> str` — `"running"` | `"paused"`
- `set_status(self, status) -> None`
- `summary(self) -> dict`

### Firestore Collections
- None (JSON-backed via `state.json`)

### Environment Variables
- `STATE_PATH` — path to state.json (default: `bluesky/reply/state.json`; Cloud Functions: `/tmp/state.json`)
- `MAX_CONVERSATION_DEPTH` — depth before forcing DM pull (default: `3`)

### Called By
- `bluesky/reply/poller.py` — instantiates `StateManager()`, calls all read/write methods
- `bluesky/reply/admin.py` — instantiates `StateManager()`, calls status/block/pause methods
- `bluesky/reply/scan_and_test.py` — imports `MAX_CONVERSATION_DEPTH` constant only

### Calls
- (pure JSON file I/O — no external calls)

---

## bluesky/reply/dm_manager.py

### Imports
- stdlib: `json`, `os`, `datetime`, `timedelta`, `timezone`, `pathlib.Path`

### Exposed API
Module-level constant:
- `CLASSIFICATION_TTL_DAYS: int` — `int(os.environ.get("CLASSIFICATION_TTL_DAYS", "30"))`

Class `DMManager`:
- `__init__(self, path=None)` — path defaults to `DM_STATE_PATH` env or `bluesky/reply/dm_state.json`
- `get_cached_user_type(self, handle) -> tuple[str, int] | tuple[None, None]` — returns `(user_type, follower_count)` if fresh, else `(None, None)`
- `cache_user_type(self, handle, user_type, follower_count) -> None`
- `get_last_checked_at(self) -> str | None` — ISO UTC timestamp
- `update_last_checked_at(self) -> None`

### Firestore Collections
- None (JSON-backed via `dm_state.json`)

### Environment Variables
- `DM_STATE_PATH` — path to dm_state.json (default: `bluesky/reply/dm_state.json`; Cloud Functions: `/tmp/dm_state.json`)
- `CLASSIFICATION_TTL_DAYS` — cache TTL in days (default: `30`)

### Called By
- `bluesky/reply/poller.py` — instantiates `DMManager()`, calls `get_cached_user_type()`, `cache_user_type()`, `get_last_checked_at()`, `update_last_checked_at()`

### Calls
- (pure JSON file I/O — no external calls)

---

## bluesky/reply/creator_classifier.py

### Imports
- stdlib: `os`

### Exposed API
Module-level constants:
- `CREATOR_FOLLOWER_THRESHOLD: int` — default `500`
- `COLLAB_DM_THRESHOLD: int` — default `20000`
- `BOT_SCORE_THRESHOLD: int` — `50`
- `BOT_SCORE_SKIP: int` — `5`

Class `UserClassification`:
- `__init__(self, user_type, follower_count, signal="none")`
- `is_creator` — property, True when `user_type == "creator"`
- `__repr__(self)`

Functions:
- `classify_user(profile, flags=None) -> UserClassification` — classifies to `"studio"` | `"themed"` | `"creator"` | `"fan"`
- `bot_score(profile) -> int` — returns 0–11 bot likelihood score; threshold `BOT_SCORE_SKIP = 5`
- `classify_replier(profile, flags=None) -> UserClassification` — alias for `classify_user`

### Firestore Collections
- None

### Environment Variables
- `CREATOR_FOLLOWER_THRESHOLD` — follower count for creator detection (default: `500`)
- `COLLAB_DM_THRESHOLD` — follower threshold for high-follower peer routing (default: `20000`)
- `CREATOR_DETECTION_MUTUAL_FOLLOW` — bool flag (default: `"false"`)
- `CREATOR_DETECTION_BIO` — bool flag (default: `"false"`)
- `CREATOR_DETECTION_FOLLOWER_COUNT` — bool flag (default: `"false"`)
- `STUDIO_HANDLES` — comma-separated handle list (no `@`)
- `THEMED_HANDLES` — comma-separated handle list (no `@`)

### Called By
- `bluesky/reply/poller.py` — `classify_user()` via `_classify_user()` wrapper, `bot_score()`
- `bluesky/reply/scan_and_test.py` — `classify_user()`

### Calls
- (pure logic — no external calls)

---

## bluesky/reply/reply_generator.py

### Imports
- stdlib: `os`, `re`
- third-party: `anthropic`
- internal: `bluesky.shared.firestore_client.db`, `bluesky.shared.cost_calculator.write_cost_event`

### Exposed API
Module-level constants:
- `GATED_POST_TYPES = {"personal", "casual"}`
- `PITCH_INTENT = {"buying_signal", "curious"}`

Functions:
- `load_brand_voice() -> str` — loads from Firestore `_system/brand_voice`, then `BRANDVOICE_CONTENT` env, then `BRANDVOICE_PATH` file
- `classify_post_type(post_text) -> str` — returns `"promotional"` | `"content"` | `"personal"` | `"casual"`
- `classify_fan_intent(reply_text) -> str` — returns `"buying_signal"` | `"curious"` | `"casual"` | `"negative"`
- `classify_peer_intent(reply_text) -> str` — returns `"compliment"` | `"dm_seeking"` | `"general"`
- `classify_subscriber_mention(reply_text) -> bool`
- `generate_reply(original_text, reply_text, handle, brand_voice, nudge=False) -> str`
- `generate_dm_pull_reply(original_text, reply_text, handle, brand_voice, used_pulls=None, discount=None) -> str`
- `generate_discount_pull_reply(original_text, reply_text, handle, brand_voice) -> str` — **exactly 4 positional args, no optionals**
- `generate_peer_reply(reply_text, handle, follower_count, brand_voice, collab_threshold=None) -> tuple[list[str], str]` — returns `(options_list, intent)`
- `generate_reply(original_text, reply_text, handle, brand_voice, nudge=False) -> str`
- `generate_studio_thanks(original_text, reply_text, handle, brand_voice) -> str`
- `generate_themed_reply(original_text, reply_text, handle, brand_voice) -> str`
- `generate_subscriber_thanks(original_text, reply_text, handle, brand_voice) -> str`
- `simulate_fan_reply(sean_reply) -> str`
- `_get_approved_examples(vehicle: str, interaction_type: str, limit: int = 4) -> list` — queries `_system/tone_review_feedback/records` subcollection; filters by vehicle+interaction_type; buckets ≤2 per fan_intent; returns up to `limit` examples
- `_few_shot_block(examples: list) -> str` — formats approved examples as "Given this fan message: … Here was an approved reply: …" style-reference block; returns `""` when list is empty

### Firestore Collections
- READ: `_system/brand_voice` — fields: `rendered_md` or `content` (first try), `updated_at`
- READ: `_system/tone_review_feedback/records` — subcollection; fields: `vehicle`, `interaction_type`, `fan_intent`, `fan_message`, `approved_text`, `at` — queried by `_get_approved_examples()` for few-shot prompt injection
- WRITE: `api_cost_events` — via `write_cost_event()`

### Environment Variables
- `ANTHROPIC_API_KEY` — Anthropic API key (consumed by `anthropic.Anthropic()`)
- `BRANDVOICE_CONTENT` — inline brand voice string (fallback if Firestore doc absent)
- `BRANDVOICE_PATH` — absolute path to brand voice file (second fallback)

### Called By
- `bluesky/reply/poller.py` — all generate/classify functions; `load_brand_voice()`
- `bluesky/engagement/fan_pipeline.py` — `classify_fan_intent()`, `classify_subscriber_mention()`
- `bluesky/reply/scan_and_test.py` — generate/classify functions; `load_brand_voice()`

### Calls
- External: `anthropic.Anthropic().messages.create(model="claude-sonnet-4-6", ...)`
- `bluesky.shared.cost_calculator.write_cost_event(db, model, usage, call_type)`
- `bluesky.shared.firestore_client.db` — `_system/brand_voice`

---

## bluesky/reply/dm_generator.py

### Imports
- stdlib: `json`, `os`
- third-party: `anthropic`
- internal: `bluesky.shared.firestore_client.db`, `bluesky.shared.cost_calculator.write_cost_event`

### Exposed API
Functions:
- `generate_like_dm(handle, post_text, brand_voice, continuation_message=None, discount=None) -> str`
- `generate_repost_dm(handle, post_text, brand_voice, continuation_message=None, discount=None) -> str`
- `generate_creator_repost_dm(handle, post_text, brand_voice, continuation_message=None) -> str` — **no `discount` param**
- `generate_themed_repost_dm(handle, post_text, brand_voice, continuation_message=None) -> str`
- `generate_studio_repost_dm(handle, post_text, brand_voice) -> str` — **3 args only, no `continuation_message`**
- `_score_thread_signal(fan_messages: list) -> tuple[int, str]` — returns `(score_0_to_10, tier)` where tier ∈ `{"low","medium","high"}`
- `_cta_instruction(exchange_count: int) -> str` — returns CTA prompt fragment or `""`
- `generate_dm_subscriber_reply(handle, their_message, history, brand_voice) -> str`
- `generate_dm_funnel_reply(handle, their_message, history, brand_voice, discount=None) -> str`
- `generate_conversation_reply(handle, their_message, history, brand_voice, exchange_count=0) -> str`
- `_get_approved_examples(vehicle: str, interaction_type: str, limit: int = 4) -> list` — duplicate of the helper in `reply_generator.py`; queries `_system/tone_review_feedback/records` subcollection for few-shot examples
- `_few_shot_block(examples: list) -> str` — duplicate of the helper in `reply_generator.py`; formats approved examples as style-reference block

**Signature asymmetry (intentional):** `generate_studio_repost_dm` takes 3 positional args; all other `generate_*_repost_dm` functions take 4. Do not add `continuation_message` to studio without updating all call sites.

### Firestore Collections
- READ: `_system/tone_review_feedback/records` — subcollection; fields: `vehicle`, `interaction_type`, `fan_intent`, `fan_message`, `approved_text`, `at` — queried by `_get_approved_examples()` for few-shot prompt injection into `generate_conversation_reply` and `generate_dm_funnel_reply`
- WRITE: `api_cost_events` — via `write_cost_event()`

### Environment Variables
- `ANTHROPIC_API_KEY` — Anthropic API key
- `FAN_DISCOUNT_URL_REPLY` — fallback tracking URL for DM CTA
- `FAN_DISCOUNT_URL_DM` — preferred tracking URL for DM conversation CTA
- `FAN_DISCOUNT_CODE` — discount code string

### Called By
- `bluesky/engagement/fan_pipeline.py` — all `generate_*_dm` functions, `generate_dm_subscriber_reply`, `generate_dm_funnel_reply`, `generate_conversation_reply`

### Calls
- External: `anthropic.Anthropic().messages.create(model="claude-sonnet-4-6", ...)`
- `bluesky.shared.cost_calculator.write_cost_event(db, model, usage, call_type)`

---

## bluesky/reply/poller.py

### Imports
- stdlib: `argparse`, `hashlib`, `os`, `sys`, `time`, `datetime`, `Path`, `random`
- third-party: `dotenv.load_dotenv`
- internal: `bluesky.shared.bluesky_client.BlueskyClient`, `bluesky.shared.firestore_client.db`, `bluesky.shared.rate_limiter` (multiple), `bluesky.shared.cost_calculator.write_cost_event`, `bluesky.engagement.fan_pipeline.queue_dm`, `bluesky.engagement.fan_pipeline.poll_inbound_dms`, `bluesky.reply.reply_generator` (all functions), `bluesky.reply.state_manager.StateManager`, `bluesky.reply.creator_classifier` (multiple), `bluesky.reply.dm_manager.DMManager`, `bluesky.reply.dm_generator`

### Exposed API
- `run_once(client, state, brand_voice, dry_run, dm_state=None) -> dict` — single notification polling cycle
- `main()` — CLI entry point

Module-level constant:
- `HANDLERS = {"reply": "_handle_reply"}`

### Key Internal Call Sites
- `generate_discount_pull_reply(root_text, reply_text, handle, brand_voice)` — line 300, **4 positional args**
- `queue_dm(handle, fan_did, "comment_exchange", root_text, "fan", interaction_at=now_iso)` — line 365
- `poll_inbound_dms(client, brand_voice, dry_run=args.dry_run)` — lines 600, 604

### Firestore Collections
- READ: `seen_events` — via `_is_seen(uri)`: `.document(id).get()`
- WRITE: `seen_events` — via `_mark_seen(uri)`: `.document(id).set({...})`
- READ: `conversations` — checks `human_handoff` field for fan routing
- WRITE: `conversations` — sets `trigger_context`, `stage`, `dm_pull_sent`
- WRITE: `engagement_events` — via `_write_engagement_event()`; now includes `fan_message` and `bot_reply` fields on all outbound reply events (fan, studio, peer, themed, subscriber)

### Environment Variables
- `BLUESKY_HANDLE` — used for self-reply detection
- `DM_ENABLED` — enables/disables DM queueing for likes/reposts (default: `"true"`)
- `K_SERVICE` — Cloud Run env var; gates human-pacing sleep

### Called By
- `functions/main.py` — `poll_notifications` CF calls `run_once()`
- Direct CLI via `python bluesky/reply/poller.py`

### Calls
- `BlueskyClient` — `.login()`, `.get_reply_notifications()`, `.get_engagement_notifications()`, `.get_profile()`, `.get_post()`, `.post_reply()`
- `StateManager` — all methods
- `DMManager` — `.get_cached_user_type()`, `.cache_user_type()`, `.get_last_checked_at()`, `.update_last_checked_at()`
- `reply_generator` — `classify_post_type()`, `classify_fan_intent()`, `classify_subscriber_mention()`, `generate_reply()`, `generate_dm_pull_reply()`, `generate_discount_pull_reply()`, `generate_peer_reply()`, `generate_subscriber_thanks()`, `generate_studio_thanks()`, `generate_themed_reply()`, `simulate_fan_reply()`, `load_brand_voice()`
- `creator_classifier` — `classify_user()`, `bot_score()`
- `rate_limiter` — `check_write()`, `seconds_until_next_write()`, `is_active_hours()`
- `fan_pipeline` — `queue_dm()`, `poll_inbound_dms()`
- `write_cost_event()` — cost tracking
- Firestore `db` — `seen_events`, `engagement_events`, `conversations`

---

## bluesky/reply/admin.py

### Imports
- stdlib: `sys`, `pathlib.Path`
- third-party: `dotenv.load_dotenv`
- internal: `bluesky.reply.state_manager.StateManager`, `bluesky.shared.firestore_client.db`

### Exposed API
- `main()` — CLI dispatcher

### CLI Commands
- `status` — print bot status
- `pause-all` — `state.set_status("paused")`
- `resume` — `state.set_status("running")`
- `pause-user @handle` — `state.pause_user(handle)`
- `block-user @handle` — `state.block_user(handle)`
- `unblock-user @handle` — `state.unblock_user(handle)`
- `clear-handoff @handle` — Firestore `conversations.document(handle).update({human_handoff: False, handoff_reason: None})`

### Firestore Collections
- WRITE: `conversations` — `human_handoff`, `handoff_reason` fields (via `clear-handoff` command)

### Environment Variables
- `STATE_PATH` — via `StateManager`

### Called By
- Direct CLI: `python bluesky/reply/admin.py <command>`

### Calls
- `StateManager` — status/block/pause methods
- Firestore `db` — `conversations` collection

---

## bluesky/reply/scan_and_test.py

### Imports
- stdlib: `argparse`, `sys`, `datetime`, `timezone`, `timedelta`, `pathlib.Path`
- third-party: `dotenv.load_dotenv`, `anthropic`
- internal: `bluesky.shared.bluesky_client.BlueskyClient`, `bluesky.reply.reply_generator` (all), `bluesky.reply.state_manager.MAX_CONVERSATION_DEPTH`, `bluesky.reply.creator_classifier`

### Exposed API
- `main()` — CLI entry point

### Firestore Collections
- None (development tool; reads from Bluesky API only)

### Environment Variables
- All env vars consumed by `BlueskyClient`, `reply_generator`

### Called By
- Direct CLI: `python bluesky/reply/scan_and_test.py`

### Calls
- `BlueskyClient` — `.login()`, `.get_author_feed()`, post fetch methods
- `reply_generator` — all classify/generate functions
- `creator_classifier` — `classify_user()`

---

## bluesky/engagement/handoff.py

### Imports
- stdlib: `re`
- third-party: `anthropic`
- internal: `bluesky.shared.firestore_client.db`, `bluesky.shared.cost_calculator.write_cost_event`

### Exposed API
- `check_handoff_triggers(message_text, exchange_count, ai_confidence=None) -> tuple[bool, str | None]` — returns `(should_handoff, reason)` where reason ∈ `{"real_person_question", "pricing_or_custom", "distress_or_abuse", "max_exchanges", "low_ai_confidence", None}`
- `flag_handoff(handle, reason) -> None` — writes to `conversations`

### Handoff Threshold
- `exchange_count >= 10` → `"max_exchanges"` trigger

### Firestore Collections
- WRITE: `conversations` — `human_handoff: True`, `handoff_reason: str`
- WRITE: `api_cost_events` — via `write_cost_event()` (semantic check Claude call)

### Environment Variables
- `ANTHROPIC_API_KEY` — for semantic real-person check

### Called By
- `bluesky/engagement/fan_pipeline.py` — `check_handoff_triggers(their_message, exchange_count)` line 794; `flag_handoff(handle, reason)` line 796

### Calls
- External: `anthropic.Anthropic().messages.create()` — semantic real-person check only
- `write_cost_event(db, model, usage, "handoff_check")`
- Firestore `db` — `conversations`

---

## bluesky/engagement/fan_pipeline.py

### Imports
- stdlib: `os`, `random`, `time`, `datetime`, `timezone`, `timedelta`, `date`, `zoneinfo.ZoneInfo`
- third-party: `google.cloud.firestore_v1.base_query.FieldFilter`
- internal: `bluesky.shared.firestore_client.db`, `bluesky.shared.cost_calculator.write_cost_event`, `bluesky.shared.rate_limiter` (multiple), `bluesky.reply.dm_generator` (all generate functions), `bluesky.reply.reply_generator.classify_fan_intent`, `bluesky.reply.reply_generator.classify_subscriber_mention`, `bluesky.engagement.handoff.check_handoff_triggers`, `bluesky.engagement.handoff.flag_handoff`

### Exposed API
Module-level constants:
- `DAILY_DM_CAP: int` — `int(os.environ.get("DAILY_DM_CAP", "50"))`
- `PRIORITY_MAP = {"follow": 3, "repost": 2, "like": 1, "comment_exchange": 2}`

Functions:
- `queue_dm(fan_handle, fan_did, trigger_type, post_context, user_type, interaction_at=None, post_created_at=None) -> None` — writes to `dm_queue`
- `send_engagement_dm(client, handle, fan_did, trigger_type, post_context, user_type, brand_voice, dry_run=False) -> str` — generates and sends DM immediately; returns status string
- `process_dm_queue_eligibility() -> dict` — pre-screens pending `dm_queue` items; marks already-DMed as skipped
- `process_dm_queue(client, brand_voice, batch_size=15, dry_run=False) -> dict` — batch follow DM executor
- `execute_engagement_dm_queue(client, brand_voice, batch_size=10, dry_run=False) -> dict` — drains like/repost/comment_exchange queue
- `poll_inbound_dms(client, brand_voice, dry_run=False) -> dict` — checks active DM conversations for fan replies

### Key Internal Call Sites
- `generate_studio_repost_dm(handle, post_context, brand_voice)` — line 97, **3 args** (no continuation)
- `generate_themed_repost_dm(handle, post_context, brand_voice, continuation)` — line 99
- `generate_creator_repost_dm(handle, post_context, brand_voice, continuation)` — line 101
- `generate_repost_dm(handle, post_context, brand_voice, continuation)` — line 103
- `generate_like_dm(handle, post_context, brand_voice, continuation)` — line 105
- `client.send_dm(convo_id, dm_text)` — lines 123, 330, 656, 842
- `check_handoff_triggers(their_message, exchange_count)` — line 794
- `flag_handoff(handle, reason)` — line 796
- `generate_dm_subscriber_reply(handle, their_message, history, brand_voice)` — line 812
- `generate_dm_funnel_reply(handle, their_message, history, brand_voice, discount=discount)` — lines 817, 822
- `generate_conversation_reply(handle, their_message, history, brand_voice, exchange_count=0)` — line 827

### Firestore Collections
- READ: `dm_queue` — status, trigger_type, fan_handle, interaction_at, fan_did, post_context, user_type
- WRITE: `dm_queue` — add new items; update status to `"sent"` / `"skipped"`
- READ: `conversations` — check existence for `_already_dmed()`; read stage, discount_sent, exchange history
- WRITE: `conversations` — set stage, trigger_context, discount_sent, discount_sent_at, human_handoff
- WRITE: `conversations/{handle}/messages` — subcollection, role + content
- READ/WRITE: `engagement_events` — write DM send events (incl. `fan_message` on inbound; outbound `dm_conversation_reply` events with `fan_message`+`bot_reply` after `poll_inbound_dms` reply); read for `_snapshot_my_posts()`

### Environment Variables
- `DAILY_DM_CAP` — daily DM cap (default: `50`)
- `BLUESKY_HANDLE` — bot handle for self-detection in `_snapshot_my_posts`
- `FAN_DISCOUNT_CODE` — discount code; also reads legacy `DISCOUNT_OFFER` as fallback
- `FAN_DISCOUNT_URL_DM` — preferred DM CTA URL
- `FAN_DISCOUNT_URL_REPLY` — fallback CTA URL

### Called By
- `bluesky/reply/poller.py` — `queue_dm()`, `poll_inbound_dms()`
- `functions/main.py` — `execute_engagement_dm_queue()`, `process_dm_queue_eligibility()`, `process_dm_queue()`, `poll_inbound_dms()`

### Calls
- `dm_generator` — all generate functions
- `reply_generator` — `classify_fan_intent()`, `classify_subscriber_mention()`
- `handoff` — `check_handoff_triggers()`, `flag_handoff()`
- `rate_limiter` — `check_dm_write()`, `seconds_until_next_dm_write()`, `is_active_hours()`
- `BlueskyClient` — `.get_dm_convo_status()`, `.send_dm()`, `.list_convos()`, `.get_author_feed()`
- Firestore `db` — all collections listed above

---

## bluesky/engagement/comment_engine.py

### Imports
- stdlib: `os`, `random`, `time`, `datetime`, `timezone`, `date`
- third-party: `anthropic`, `google.cloud.firestore_v1.base_query.FieldFilter`
- internal: `bluesky.shared.firestore_client.db`, `bluesky.shared.cost_calculator.write_cost_event`, `bluesky.shared.rate_limiter` (multiple)

### Exposed API
Module-level constants:
- `ENGAGEMENT_MIN = 15` — minimum likes+reposts to qualify a post
- `ENGAGEMENT_MAX = 150` — maximum (avoids mega-posts)
- `DAILY_COMMENT_CAP: int` — `int(os.environ.get("DAILY_COMMENT_CAP", "50"))`

Functions:
- `scan_target_posts(client) -> dict` — fetches recent posts from Tier 1/2 target accounts; queues qualifying posts
- `execute_comment_queue(client, brand_voice, dry_run=False) -> dict` — dequeues next pending comment; generates and posts it

### Firestore Collections
- READ: `target_accounts` — tier, domains, handle
- READ: `comment_queue` — status, post_uri, comment_text
- WRITE: `comment_queue` — add new items; update status to `"posted"` / `"skipped"`
- WRITE: `engagement_events` — `type="comment"`, `direction="outbound"`
- WRITE: `api_cost_events` — via `write_cost_event()`

### Environment Variables
- `DAILY_COMMENT_CAP` — daily comment cap (default: `50`)
- `ANTHROPIC_API_KEY` — for comment generation

### Called By
- `functions/main.py` — `scan_target_posts()`, `execute_comment_queue()`

### Calls
- External: `anthropic.Anthropic().messages.create()`
- `rate_limiter` — `check_read()`, `check_write()`, `seconds_until_next_write()`
- `BlueskyClient` — `.get_author_feed()`, `.post_reply()` (or equivalent post method)
- `write_cost_event(db, model, usage, "comment_generation")`
- Firestore `db`

---

## bluesky/engagement/discovery.py

### Imports
- stdlib: `math`, `statistics`, `time`, `datetime`, `timezone`
- third-party: `google.cloud.firestore_v1.base_query.FieldFilter`
- internal: `bluesky.shared.firestore_client.db`, `bluesky.shared.rate_limiter.check_read`, `bluesky.shared.rate_limiter.RateLimitError`

### Exposed API
Module-level references:
- `_TARGET = db.collection("target_accounts")`
- `_GRAPH_STATE = db.collection("_system").document("follower_graph_state")`

Functions:
- `discover_starter_packs(client, domain_keywords, domains=None, pack_limit=10, member_cap=500) -> dict`
- `prefetch_fan_profiles(client, creator_handle, cap=10000) -> dict` — Phase A: fetch/filter fan profiles
- `analyze_follower_graph_slot(client, creator_handle, slot=0, slot_size=2000, followee_cap=500, top_pct=0.20) -> dict` — Phase B: process one slot
- `score_and_tier() -> dict` — assigns tier 1/2/3 and combined scores

### Firestore Collections
- READ/WRITE: `target_accounts` — all fields: handle, tier, score, domains, discovery_sources, follower_graph_score, starter_pack_score, etc.
- READ/WRITE: `_system/follower_graph_state` — fields: `fan_dids`, `statistics`

### Environment Variables
- None at module level (all parameters injected by `functions/main.py`)

### Called By
- `functions/main.py` — all four public functions

### Calls
- `BlueskyClient` — `.get_followers_page()`, `.get_follows_page()`, `.search_starter_packs()`, `.get_starter_pack()`, `.get_list_members_page()`
- `rate_limiter` — `check_read()`
- Firestore `db`

---

## bluesky/web/api/brand_voice.py

### Imports
- stdlib: (none)

### Exposed API
- `render_brand_voice_md(doc: dict) -> str` — renders a Firestore `_system/brand_voice` document dict into a prompt-ready markdown string

### Firestore Collections
- None (receives doc dict as parameter)

### Environment Variables
- None

### Called By
- `bluesky/web/api/main.py` — `GET /api/brand-voice`, `POST /api/brand-voice/preview`

### Calls
- (pure Python — no external calls)

---

## bluesky/web/api/main.py

### Imports
- stdlib: `hashlib`, `json`, `os`, `random`, `sys`, `time`, `urllib.parse`, `urllib.request`, `datetime`, `timedelta`, `timezone`, `ZoneInfo`, `Any`, `Optional`
- third-party: `anthropic`, `firebase_admin`, `firebase_admin.auth`, `dotenv.load_dotenv`, `fastapi.*`, `google.cloud.firestore_v1.base_query.FieldFilter`
- internal: `bluesky.shared.cost_calculator.write_cost_event`, `bluesky.shared.firestore_client.db`, `bluesky.web.api.brand_voice.render_brand_voice_md`

### Exposed API
FastAPI app with 38 HTTP endpoints. Auth required on all endpoints via `_auth()` dependency (Firebase ID token or `DASHBOARD_SECRET` fallback).

Key endpoint groups:
- Health: `GET /api/health`, `GET /api/errors`, `GET /api/caps`
- Settings: `GET /api/settings`, `POST /api/settings`
- Analytics: `GET /api/funnel`, `GET /api/growth`, `GET /api/audience`, `GET /api/heatmap`, `GET /api/activity`, `GET /api/posts`, `GET /api/posts/{uri}`, `GET /api/insights`, `GET /api/dm-effectiveness`
- Engagement ops: `GET /api/handoff`, `GET /api/handoff/{handle}`, `POST /api/handoff/{handle}/resolve`, `POST /api/activity/resume/{handle}`
- Tone review: `GET /api/tone-review`, `POST /api/tone-review/{item_id}/feedback` (stores `fan_message`, `vehicle`, `interaction_type`, `fan_intent`, `approved_text` alongside feedback record), `POST /api/tone-review/refresh`, `GET /api/tone-review/approved-examples` (returns all approved records with both `approved_text` and `fan_message`, ordered by `at` DESC, limit 200), `PATCH /api/tone-review/approved-examples/{record_id}` (update `approved_text` on a single record), `DELETE /api/tone-review/approved-examples/{record_id}` (delete a single record)
- User/DM: `GET /api/handles`, `GET /api/user/{handle}`, `POST /api/user/{handle}/dm`
- Classifier: `GET /api/classifier/{type}/stats`, `GET /api/classifier/{type}/session`, `POST /api/classifier/{type}/label`
- Brand voice: `GET /api/brand-voice`, `POST /api/brand-voice`, `GET /api/brand-voice/history`, `POST /api/brand-voice/preview`
- Query: `POST /api/query`
- Spend: `GET /api/spend/summary`, `GET /api/spend`

`GET /api/posts` params: `range` (default `7d`), `sort` (`recent`/`dm_pulls`/`replies`), `type` (`all`/`promo`/`personal`), `period` (optional bucket label — when provided with `range`, filters to that exact time bucket via `_range_buckets()`). Content page always passes `range=30d`; shift+click filter passes the clicked bucket label + chart range. Seeds post list from `type="post"` engagement events only (Sean's own posts); overlays engagement counts from all events. Attaches `image_url` via `_attach_image_urls()`.

`GET /api/posts/{uri}` — single post detail; returns `hourly_replies`, `nudge_intent_rate_pct`, `engagement_peak_offset_hrs`, `image_url`. Fetches image from cache or public Bluesky API if missing.

Post-cache helpers (module-level, not endpoints):
- `_load_post_cache()` — reads `_system/post_cache` → `cache` map field
- `_save_post_cache(cache)` — writes `_system/post_cache` → `cache` map field
- `_fetch_post_images(uris)` — batch GETs `public.api.bsky.app/xrpc/app.bsky.feed.getPosts` (25 per request, no auth); extracts `thumbnail` from `video#view` or `thumb` from `images#view`
- `_attach_image_urls(posts)` — loads cache, fetches uncached URIs, saves, mutates post dicts in place

### Firestore Collections
- READ: `engagement_events`, `conversations`, `conversations/{handle}/messages`, `function_runs`, `dm_queue`, `comment_queue`, `target_accounts`, `seen_events`, `api_cost_events`
- READ: `_system/settings`, `_system/rate_state`, `_system/tone_review_queue`, `_system/tone_review_feedback`, `_system/insights_cache`, `_system/bluesky_session`, `_system/brand_voice`, `_system/follower_snapshots/daily/{date}`, `_system/classifier_stats`, `_system/classifier_labels`, `_system/post_cache`
- WRITE: `conversations` — `pending_manual_reply`, `has_pending_manual_reply`, `human_handoff`
- WRITE: `_system/settings` — partial merge via `POST /api/settings`
- WRITE: `_system/tone_review_queue`, `_system/tone_review_feedback`
- WRITE: `_system/insights_cache`
- WRITE: `_system/brand_voice` — via `POST /api/brand-voice`
- WRITE: `_system/classifier_stats`, `_system/classifier_labels`
- WRITE: `_system/post_cache` — written by `_attach_image_urls()` on cache miss; key=`cache`, value = map of URI → image_url (null for posts with no media embed)
- WRITE: `api_cost_events` — via `write_cost_event()` for `/api/query` calls

### Environment Variables
- `K_SERVICE` — Cloud Run detection (sets production CORS)
- `GOOGLE_CLOUD_PROJECT` — GCP project (default: `"sx-platform"`)
- `DASHBOARD_ORIGIN` — allowed origin in production (default: `"https://sx-platform.web.app"`)
- `DASHBOARD_SECRET` — fallback Bearer token auth
- `STATE_PATH` — path to legacy state.json (for `_read_state()`)
- `BRANDVOICE_PATH` — brand voice file path
- `MONTHLY_SPEND_CAP_USD` — optional spend cap
- `MAX_CONVERSATION_DEPTH` — default `3`
- `MAX_DISCOUNTS_PER_DAY` — default `5`
- `DAILY_COMMENT_CAP` — default `50`
- `DAILY_DM_CAP` — default `50`
- `CREATOR_DETECTION_MUTUAL_FOLLOW`, `CREATOR_DETECTION_BIO`, `CREATOR_DETECTION_FOLLOWER_COUNT`
- `CREATOR_FOLLOWER_THRESHOLD`, `COLLAB_DM_THRESHOLD`
- `STUDIO_HANDLES`, `THEMED_HANDLES`
- `FAN_DISCOUNT_CODE`, `FAN_DISCOUNT_URL_REPLY`, `FAN_DISCOUNT_URL_LIKE`, `FAN_DISCOUNT_URL_REPOST`
- `ANTHROPIC_API_KEY` — for `/api/query` and tone review

### Called By
- HTTP clients (browser dashboard, curl)
- `run_local.sh` — starts uvicorn locally

### Calls
- `bluesky.shared.firestore_client.db` — all collections
- `bluesky.shared.cost_calculator.write_cost_event()`
- `bluesky.web.api.brand_voice.render_brand_voice_md()`
- External: `firebase_admin.auth.verify_id_token()`, `anthropic.Anthropic().messages.create()`
- External: `https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts` — unauthenticated batch post fetch for image/thumbnail URLs (called by `_fetch_post_images()`)

---

## functions/main.py

### Imports
- stdlib: `os`, `sys`, `time`
- third-party: `functions_framework`, `dotenv.load_dotenv`
- internal (dynamic, inside handlers):
  - `bluesky.shared.bluesky_client.BlueskyClient`
  - `bluesky.reply.reply_generator.load_brand_voice`
  - `bluesky.shared.activity_logger.log_run`
  - `bluesky.reply.poller.run_once`
  - `bluesky.engagement.fan_pipeline.*`
  - `bluesky.engagement.comment_engine.*`
  - `bluesky.engagement.discovery.*`

### Exposed API (Cloud Function Entry Points)
13 `@functions_framework.http` handlers:
- `poll_notifications(request)` — calls `run_once()`; every 5 min
- `poll_inbound_dms(request)` — calls `poll_inbound_dms()`; every 3 min
- `scan_comment_targets(request)` — calls `scan_target_posts()`; every 15 min
- `execute_comment(request)` — calls `execute_comment_queue()`; every 20 min
- `execute_engagement_dms(request)` — calls `execute_engagement_dm_queue()`; every 5 min
- `process_dm_queue(request)` — calls `process_dm_queue_eligibility()`; every 2 hours
- `execute_dm_batch(request)` — calls `process_dm_queue()`; every 4 hours
- `follower_graph_prefetch(request)` — calls `prefetch_fan_profiles()`; weekly
- `follower_graph_slot(request)` — calls `analyze_follower_graph_slot()`; nightly slots
- `follower_graph_score(request)` — calls `score_and_tier()`; weekly
- `starter_pack_discovery(request)` — calls `discover_starter_packs()`; weekly
- `cleanup_stale_docs(request)` — deletes stale `seen_events`, `dm_queue`, `comment_queue`, `function_runs`; weekly
- `snapshot_follower_count(request)` — writes to `_system/follower_snapshots/daily/{date}`; nightly

Internal helper:
- `_log(fn_name, metrics, start, error=None)` — calls `log_run(fn_name, metrics, status, error_msg, duration_s)`
- `_client()` — instantiates and logs in `BlueskyClient`
- `_brand_voice()` — calls `load_brand_voice()`

### Firestore Collections
- DELETE: `seen_events` (>7 days), `dm_queue` (sent/skipped >30 days), `comment_queue` (posted/skipped >30 days), `function_runs` (>90 days) — via `cleanup_stale_docs`
- WRITE: `_system/follower_snapshots` subcollection `daily` — via `snapshot_follower_count`

### Environment Variables (all consumed by downstream modules)
- `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD`, `ANTHROPIC_API_KEY`, `BRANDVOICE_CONTENT`, `GOOGLE_CLOUD_PROJECT`, `FIRESTORE_DATABASE`
- `CREATOR_DETECTION_MUTUAL_FOLLOW`, `CREATOR_DETECTION_BIO`, `CREATOR_DETECTION_FOLLOWER_COUNT`
- `COLLAB_DM_THRESHOLD`, `DAILY_DM_CAP`, `DAILY_COMMENT_CAP`, `CLASSIFICATION_TTL_DAYS`
- `FAN_DISCOUNT_CODE`, `FAN_DISCOUNT_URL_LIKE`, `FAN_DISCOUNT_URL_REPOST`
- `STATE_PATH=/tmp/state.json`, `DM_STATE_PATH=/tmp/dm_state.json`
- `DISCOVERY_CREATOR_HANDLE`, `DISCOVERY_DOMAIN_KEYWORDS`, `DISCOVERY_DOMAINS`
- `FOLLOWER_GRAPH_FAN_CAP`, `FOLLOWER_GRAPH_SLOT_SIZE`, `FOLLOWER_GRAPH_FOLLOWEE_CAP`, `FOLLOWER_GRAPH_TOP_PCT`

### Called By
- Cloud Scheduler (all 13 functions on their cron schedules — see `scheduler.sh`)
- `main.py` (repo root) — re-exports via `from functions.main import *`

### Calls
- All engagement, reply, and shared modules
- `activity_logger.log_run()` — via `_log()` after every handler

---

## Dependency Call Graph (Summary)

```
functions/main.py
  ├── poller.run_once()
  │     ├── BlueskyClient
  │     ├── StateManager, DMManager
  │     ├── reply_generator.*
  │     ├── creator_classifier.*
  │     ├── rate_limiter.*
  │     └── fan_pipeline.queue_dm()
  ├── fan_pipeline.poll_inbound_dms()
  │     ├── BlueskyClient.list_convos(), send_dm()
  │     ├── dm_generator.*
  │     ├── reply_generator.classify_*()
  │     ├── handoff.check_handoff_triggers(), flag_handoff()
  │     └── rate_limiter.*
  ├── fan_pipeline.execute_engagement_dm_queue()
  │     └── (same deps as poll_inbound_dms)
  ├── fan_pipeline.process_dm_queue()
  │     └── dm_generator.*, BlueskyClient.send_dm()
  ├── comment_engine.scan_target_posts()
  │     └── BlueskyClient, rate_limiter
  ├── comment_engine.execute_comment_queue()
  │     └── anthropic, BlueskyClient, rate_limiter
  ├── discovery.prefetch_fan_profiles()
  │     └── BlueskyClient, rate_limiter
  ├── discovery.analyze_follower_graph_slot()
  │     └── BlueskyClient, rate_limiter
  ├── discovery.score_and_tier()
  │     └── Firestore only
  └── discovery.discover_starter_packs()
        └── BlueskyClient, rate_limiter

Shared infrastructure (used by all):
  firestore_client.db  ←  every module
  cost_calculator      ←  reply_generator, dm_generator, handoff, comment_engine, web/api/main.py
  activity_logger      ←  functions/main.py only
  rate_limiter         ←  poller, fan_pipeline, comment_engine, discovery
```
