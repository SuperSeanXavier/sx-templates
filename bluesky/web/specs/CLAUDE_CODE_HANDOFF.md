# Claude Code handoff — SX Platform dashboard
**Start here.** Read this document, then the specs, then CLAUDE.md — in that order.

---

## What this is

A web dashboard, content analytics tool, brand voice editor, and bot control panel for the SX Platform Bluesky automation system. The backend connects to the existing Firestore database and Python bot codebase. The frontend is an approved single-file SPA mockup that needs to be wired to real data.

---

## Files to read before writing any code

| Order | File | Purpose |
|---|---|---|
| 1 | `CLAUDE.md` | Full system architecture — Firestore collections, Cloud Functions, env vars, module map |
| 2 | `web/mockups/sx-dashboard-v7.html` | Approved single-file SPA — all 7 pages |
| 3 | `web/specs/DASHBOARD_SPEC.md` | Every UI element mapped to its Firestore data source and API endpoint |
| 4 | `web/specs/BRAND_VOICE_SPEC.md` | Brand voice storage, push-to-bot flow, API endpoints |
| 5 | `web/specs/QUERY_AND_SPEND_SPEC.md` | Query bar (all pages) + financial spend tracking |

**Do not modify the mockup file.** It is the approved design — reference only.

**Note:** `sx-brand-voice.html` is superseded. The brand voice editor is now page 5 of the SPA in `sx-dashboard-v7.html`. Discard the old separate file.

---

## Pages (all in one SPA)

| Page | Sidebar icon | ID |
|---|---|---|
| Dashboard | ⊞ | `dashboard` |
| Content performance | ▦ | `content` |
| Tone review | ◎ | `tone` |
| Activity | ≡ | `activity` |
| Brand voice | ❧ | `brandvoice` |
| Spend | $ | `spend` |
| Settings | ⚙ | `settings` |

Navigation: desktop uses the 52px fixed icon sidebar. Mobile uses a slide-out drawer (220px) triggered by a hamburger button in every topbar.

---

## What to build

### Page 1 — Dashboard
- Topbar with hamburger (mobile) + time range toggle
- Persistent query bar (below topbar, all screen sizes)
- Health bar — status dot reflects global bot running/paused state (synced with Settings toggle)
- AI insights bar
- Reach & conversion card — tabbed: Conversion chart + Audience growth
- DM engagement & effectiveness chart
- Engagement heatmap (24h × 7d, toggleable)
- Human handoff queue (preview, links to Activity page)
- Tone review card (preview, links to Tone review page)
- Activity feed (preview, links to Activity page)
- Four-column bottom row: Discounts · Comments · DM outreach · API spend summary

### Page 2 — Content performance
- Topbar with sort toggle (recent / DM pulls / replies)
- Persistent query bar
- Toolbar with type filter (all / promotional / personal) and period filter badge
- Post grid — cards with placeholder image, post type tag, timestamp, reply/pull/rate stats
- Slide-in detail panel with image, full stats, hourly sparkline

### Page 3 — Tone review (tabbed)

**Tab 1 — Editorial review**
- Left sidebar: vehicle/interaction type filter list with item counts per category
- Main area: review items with fan message, bot reply, approve/flag/discuss actions
- Session history summary in sidebar footer
- "Discuss →" opens the tone chat modal (same modal as on dashboard)

**Tab 2 — Classification review**
- Four classifier cards: Fan intent · Post type · Subscriber guard · Human handoff
- Each card: accuracy %, trend direction, corrections count, labeled count
- Labeling session: button starts a one-at-a-time thumbs-up/down flow
- Progress bar tracks session completion
- Results update classifier accuracy stats

### Page 4 — Activity
- Topbar with time range toggle
- Persistent query bar
- Paused interactions section (amber, collapsible) — shows all currently paused handles with reason, duration, and per-handle resume button
- Type filter bar: all / replies / DMs / comments / flags / paused / discounts
- Expandable full feed — click any item to reveal classification context

### Page 5 — Brand voice (section editor)
- Persistent query bar
- Left nav with §1–§7 section links, dirty-state dots per section
- Section panels: Identity, Voice & register, Lexicon, Structural rules, Content rules, Platform rules, Archetypes
- Tag inputs for approved/banned vocab and core pillars
- Rule lists with add/remove for always/never rules
- Archetype cards with add/remove
- Live preview panel with regenerate variants
- Diff view appears when changes are pending
- Push bar at bottom: version badge, dirty section count, "push to bot" button

### Page 6 — Spend
- Topbar with 7d / 30d toggle
- Persistent query bar
- Four stat cards: this week, Anthropic, GCP, top driver
- Daily stacked bar chart
- By call type + by model breakdown bars
- Raw events table

### Page 7 — Settings (section nav)
- **Bot controls** — global pause/resume toggle (syncs health bar + health dot on dashboard), poll interval, max conversation depth, reply delay mode
- **Daily caps** — discounts, comments, DM outreach, monthly spend cap
- **Creator detection** — mutual follow / bio keyword / follower count signal toggles, thresholds, manual handle lists
- **Discounts & CTAs** — discount code, tracking URLs per source (reply, like, repost)
- **Notifications** — per-event alert toggles
- **Danger zone** — clear dedup state, reset classifications, flush queues (all require confirmation)

---

## FastAPI backend (`web/api/main.py`)

Reuse `bluesky/shared/firestore_client.py` for all Firestore access — do not create a second Firestore connection.

Auth: `DASHBOARD_SECRET` env var, validated as `Authorization: Bearer <secret>` on every request.

Full endpoint list:

| Endpoint | Spec file |
|---|---|
| `GET /api/health` | DASHBOARD_SPEC |
| `GET /api/funnel` | DASHBOARD_SPEC |
| `GET /api/funnel/snapshot` | DASHBOARD_SPEC |
| `GET /api/growth` | DASHBOARD_SPEC |
| `GET /api/dm-effectiveness` | DASHBOARD_SPEC |
| `GET /api/heatmap` | DASHBOARD_SPEC |
| `GET /api/handoff` | DASHBOARD_SPEC |
| `GET /api/tone-review` | DASHBOARD_SPEC |
| `POST /api/tone-review/:id/feedback` | DASHBOARD_SPEC |
| `GET /api/activity` | DASHBOARD_SPEC |
| `GET /api/activity/paused` | DASHBOARD_SPEC |
| `POST /api/activity/resume/:handle` | DASHBOARD_SPEC |
| `GET /api/caps` | DASHBOARD_SPEC |
| `GET /api/insights` | DASHBOARD_SPEC |
| `GET /api/posts` | DASHBOARD_SPEC |
| `GET /api/posts/:uri` | DASHBOARD_SPEC |
| `GET /api/user/:handle` | DASHBOARD_SPEC |
| `GET /api/classifier/:type/stats` | DASHBOARD_SPEC |
| `GET /api/classifier/:type/session` | DASHBOARD_SPEC |
| `POST /api/classifier/:type/label` | DASHBOARD_SPEC |
| `GET /api/brand-voice` | BRAND_VOICE_SPEC |
| `POST /api/brand-voice` | BRAND_VOICE_SPEC |
| `GET /api/brand-voice/history` | BRAND_VOICE_SPEC |
| `POST /api/brand-voice/preview` | BRAND_VOICE_SPEC |
| `POST /api/query` | QUERY_AND_SPEND_SPEC |
| `GET /api/spend/summary` | QUERY_AND_SPEND_SPEC |
| `GET /api/spend` | QUERY_AND_SPEND_SPEC |
| `GET /api/settings` | DASHBOARD_SPEC |
| `POST /api/settings` | DASHBOARD_SPEC |

---

## Bot instrumentation (complete before any frontend wiring)

**1. `api_cost_events` Firestore writes** — add to every `anthropic.messages.create()` call:
- `reply_generator.py`, `dm_generator.py`, `fan_pipeline.py`, `comment_engine.py`
- `web/api/main.py` (query bar, insights, brand voice preview, labeling session calls)

Centralise cost calculation in `bluesky/shared/cost_calculator.py`. See QUERY_AND_SPEND_SPEC.

**2. `engagement_events` field enrichment** — add to existing writes:
- `reply_type`, `interaction_subtype`, `fan_intent`, `mirror_tier`, `post_type_classification`
- `token_usage_input`, `token_usage_output`, `model`

See DASHBOARD_SPEC instrumentation note.

**3. `snapshot-follower-count` Cloud Scheduler job** — nightly, writes `{ date, count }` to `_system/follower_snapshots`. Add to `deploy.sh` and `scheduler.sh`.

**4. Bot startup brand voice change** — read from Firestore `_system/brand_voice` first, fallback to `BRANDVOICE_PATH`. See BRAND_VOICE_SPEC.

**5. Settings persistence** — writable settings (caps, intervals, detection flags, discount config) must be stored in Firestore `_system/settings` so `POST /api/settings` can persist changes. On bot startup, read caps and config from `_system/settings` if present, overriding env var defaults.

---

## Implementation order

Work through these steps in order. Show the plan before writing code for any step. Test before advancing.

1. **Audit + instrument Anthropic calls** — find every `messages.create()`, add `api_cost_events` writes, create `cost_calculator.py`
2. **Enrich `engagement_events` writes** — add missing fields across all generator files
3. **Add `snapshot-follower-count` job** — add to `deploy.sh` + `scheduler.sh`
4. **Create `_system/settings` Firestore doc** — seed with current env var defaults; update bot startup to read from it
5. **FastAPI skeleton** — `web/api/main.py` with auth middleware, health check, CORS
6. **`/api/health` and `/api/caps`** — simplest endpoints, good smoke test
7. **`/api/settings` GET + POST** — read/write `_system/settings`; POST also writes back to env for running process
8. **`/api/funnel` and `/api/heatmap`** — core dashboard chart data
9. **`/api/growth`** — audience growth tab (depends on step 3)
10. **`/api/dm-effectiveness` and `/api/handoff`**
11. **`/api/tone-review` GET + POST feedback** — surfacing logic + feedback writes
12. **`/api/activity` GET + `/api/activity/paused` + `POST /api/activity/resume/:handle`**
13. **`/api/classifier/:type/stats` + `/session` + `/label`** — classification review labeling loop
14. **`/api/insights`** — Claude-generated insights bar (cache in `_system/insights_cache`)
15. **`/api/posts` and `/api/posts/:uri`** — content performance page
16. **`/api/user/:handle`** — case review lookup
17. **`/api/brand-voice` GET + POST + preview + history**
18. **`/api/query`** — two-step Claude + Firestore natural language query
19. **`/api/spend/summary` and `/api/spend`**
20. **Wire dashboard page** — replace mock data with `fetch()` calls
21. **Wire content, spend pages** — replace mock data
22. **Wire tone review page** — editorial items + classification session loop
23. **Wire activity page** — feed, paused section, resume button
24. **Wire brand voice + settings pages** — editor, push flow, settings form

---

## Frontend wiring guidance (steps 20–24)

General rules:
- Set `const API_BASE = 'http://localhost:8000'` at top of script block for local dev
- Never rewrite rendering logic — all chart builds, DOM manipulation, and UI state functions exist in the mockup
- Only replace: hardcoded data arrays, `setTimeout` mock delays, and `queryResponses` array

**Dashboard wiring:**
- `buildActChart()` → fetch `/api/funnel?range=<range>`
- `buildGrowthChart()` → fetch `/api/growth?range=<range>`
- `renderDMChart()` → fetch `/api/dm-effectiveness?range=<range>&period=<period>`
- `buildHeatmap()` → fetch `/api/heatmap?mode=<mode>`
- Handoff queue → fetch `/api/handoff`
- Tone review card → fetch `/api/tone-review` (limit 3 for preview)
- Activity feed → fetch `/api/activity?range=24h` (limit 5 for preview)
- Health bar → fetch `/api/health`, poll every 30s
- Insights bar → fetch `/api/insights`
- Cap cards → fetch `/api/caps`
- Spend summary card → fetch `/api/spend/summary`

**Settings wiring:**
- On page load → fetch `GET /api/settings`, populate all form fields
- `toggleGlobalPause()` → `POST /api/settings` with `{ bot_status: 'running' | 'paused' }` → on success, update health bar dot and label
- All other settings form changes → debounce 800ms → `POST /api/settings` with changed fields
- Danger zone buttons → `POST /api/settings` with the specific reset action

**Activity page wiring:**
- On page load → fetch `/api/activity/paused` → render paused section
- `resumeHandle()` → `POST /api/activity/resume/:handle` → on success, remove item from paused section, show toast
- Feed → fetch `/api/activity?range=<range>&type=<type>` on load and on filter change

**Tone review page wiring (Editorial tab):**
- On page load → fetch `/api/tone-review` → `renderToneItems()`
- Sidebar filter click → re-fetch `/api/tone-review?vehicle=<vehicle>&interaction=<interaction>`
- `trApprove(id)` → `POST /api/tone-review/:id/feedback` with `{ action: 'approve' }`
- `trFlag(id)` → `POST /api/tone-review/:id/feedback` with `{ action: 'flag' }` → open modal

**Tone review page wiring (Classification tab):**
- On page load → fetch `/api/classifier/intent/stats`, `/api/classifier/posttype/stats`, etc. → populate each classifier card
- `startLabelSession(type)` → fetch `/api/classifier/:type/session?limit=10` → render items
- `labelAnswer(type, val)` → `POST /api/classifier/:type/label` with `{ item_id, label: true | false | null }` → advance session

**Brand voice page wiring:**
- On page load → fetch `GET /api/brand-voice` → populate all section fields
- `bvMarkDirty()` → sets dirty flag, no API call yet
- `bvPush()` → `POST /api/brand-voice` with full document → on success, clear dirty state, show toast
- `regenPreview()` → `POST /api/brand-voice/preview` with current (unsaved) doc state

---

## Polling strategy

| Data | Interval | Endpoint |
|---|---|---|
| Health bar | 30s | `/api/health` |
| Activity feed (dashboard preview) | 30s | `/api/activity` |
| Handoff queue | 60s | `/api/handoff` |
| All chart data | 5 min | `/api/funnel`, `/api/growth`, etc. |
| Insights bar | On load only (cached server-side 1hr) | `/api/insights` |

Implement polling with `setInterval` in the frontend. Clear intervals when navigating away from a page to avoid stale callbacks.

---

## Environment variables to add to `.env.example`

```
DASHBOARD_SECRET=           # Bearer token for dashboard API auth
MONTHLY_SPEND_CAP_USD=      # Optional — budget cap; amber warning at 80%, red at 100%
```

All other required env vars already exist in `bluesky/reply/.env.example`.

---

## Deployment

- FastAPI backend → Cloud Run (`web/Dockerfile` + `web/cloudbuild.yaml`)
- Static HTML SPA → Firebase Hosting (`firebase.json` + `.firebaserc`)
- Both wired into existing `deploy.sh`
- `snapshot-follower-count` job added to `scheduler.sh`

---

## Parking lot (do not build)

Email capture DM flow → discount code → manual subscriber attribution via SeanXavier.com. Deferred to v2.
