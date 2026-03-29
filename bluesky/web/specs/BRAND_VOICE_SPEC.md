# Brand voice spec — SX Platform
**For Claude Code.** Maps the brand voice editor page to its storage, push flow, and API endpoints.
Reference the approved visual mockup at `web/mockups/sx-dashboard-v7.html` — page 5 of the SPA (`page-brandvoice`).

**Note:** The old `sx-brand-voice.html` file is superseded. The brand voice editor is now fully integrated into the main SPA. Discard any separate brand voice mockup file.

---

## Storage architecture

Brand voice lives in two places that must stay in sync:

1. **Firestore `_system/brand_voice`** — source of truth for the running bot and web UI. Bot reads from here at startup (falling back to `BRANDVOICE_PATH` file if doc is absent).
2. **`SX_Instructions.md`** (path from `BRANDVOICE_PATH`) — human-readable backup, kept in sync via Secret Manager after each push.

**Push flow:**
1. User edits fields in the web UI → dirty state tracked per section
2. User clicks "push to bot"
3. Frontend POSTs full updated document to `/api/brand-voice`
4. Backend writes to Firestore `_system/brand_voice`, increments `version`, sets `pushed_at`
5. Backend calls `render_brand_voice_md(doc)` → produces markdown string
6. Backend writes rendered markdown to Secret Manager (`brandvoice-content` secret, new version)
7. On next bot invocation, startup reads fresh brand voice

---

## Firestore document structure

**Collection:** `_system` · **Document:** `brand_voice`

```json
{
  "template_version": "v1",
  "schema_version": 1,
  "version": 4,
  "pushed_at": "2026-03-28T14:00:00Z",

  "identity": {
    "creator_name": "Sean Xavier",
    "handle": "@seanxavier.bsky.social",
    "persona_summary": "I'm a creator known for...",
    "core_pillars": ["authenticity", "warmth", "directness", "intimacy"],
    "platform": "Fansly"
  },

  "voice": {
    "philosophy": "I communicate like someone who knows their worth...",
    "point_of_view": "Always first person singular..."
  },

  "lexicon": {
    "approved_vocab": ["slide in", "connect", "real talk", "energy", "vibe"],
    "banned_vocab": ["appreciate", "utilize", "delve", "certainly", "absolutely"],
    "punctuation_rules": "No exclamation marks in DM pulls...",
    "emoji_rules": "Use sparingly. Max 1 per message..."
  },

  "structural": {
    "reply_lengths": {
      "nudge": "80–140 chars",
      "dm_pull": "100–160 chars",
      "peer": "60–120 chars"
    },
    "rhythm": "Mix short declarative sentences...",
    "opening_lines": "Never open with 'Hey' or 'Hi'..."
  },

  "content_rules": {
    "always": [
      "Sound like a real person who chose to respond, not a bot that had to",
      "Acknowledge the fan's specific message before pivoting to a nudge or pull",
      "Leave the door open — end with a question or observation"
    ],
    "never": [
      "Make promises about content ('I'll post X soon')",
      "Ask more than one question in a single reply",
      "Use the word 'content' — say 'what I make', 'my stuff'",
      "Reference price or subscription cost in a public reply",
      "Claim to be a human if sincerely asked"
    ]
  },

  "platform_rules": {
    "bluesky_public_reply_tone": "Bluesky leans more intellectual...",
    "thread_behaviour": "On promo posts, max 3 exchanges...",
    "dm_vs_public": "DMs are warmer, more personal...",
    "comment_engine_tone": "Comments should feel like a real fan..."
  },

  "archetypes": [
    {
      "id": "archetype_1",
      "name": "The lurker",
      "signals": "low follower count, no prior interaction, first-time reply",
      "opener_style": "Open with a warm, low-pressure acknowledgment..."
    }
  ]
}
```

---

## UI sections (§1–§7 matching the SPA)

| Section | Key fields |
|---|---|
| §1 Identity | `creator_name`, `handle`, `persona_summary`, `core_pillars` (tag input), `platform` |
| §2 Voice & register | `philosophy`, `point_of_view` |
| §3 Lexicon | `approved_vocab` (green tag input), `banned_vocab` (red tag input), `punctuation_rules`, `emoji_rules` |
| §4 Structural rules | `reply_lengths` (3 inputs), `rhythm`, `opening_lines` |
| §5 Content rules | `always` (rule list with add/remove), `never` (rule list with add/remove) |
| §6 Platform extensions | `bluesky_public_reply_tone`, `thread_behaviour`, `comment_engine_tone` |
| §7 Archetypes | Array of archetype cards, each with `name`, `signals`, `opener_style` |

**Dirty state tracking:** Each section independently tracks whether it has unsaved changes. Dirty sections show an amber dot in the left nav. The diff view at the bottom of §7 (or wherever the user last edited) lists all dirty sections. All dirty sections are included in a single `POST /api/brand-voice` call — partial section saves are not supported.

**Live preview** (bottom of §1): Shows two examples (nudge reply + DM outreach). `regenPreview(type)` cycles through stored variants client-side — does NOT call the API on each click. A full regeneration (calling `POST /api/brand-voice/preview`) only fires when the user explicitly opens the preview panel with unsaved changes. This keeps preview cost low.

---

## API endpoints

### GET `/api/brand-voice`
Returns current document from Firestore `_system/brand_voice`. If doc doesn't exist, reads from `BRANDVOICE_PATH` file and returns it (does not auto-create the Firestore doc).

**Response:** Full document as described above, plus `version` and `pushed_at`.

---

### POST `/api/brand-voice`
Saves and pushes updated brand voice.

**Validation:** Required fields: `identity.creator_name`, `identity.handle`, `identity.persona_summary`.

**Steps:**
1. Validate required fields
2. Increment `version`, set `pushed_at = now()`
3. Write to Firestore `_system/brand_voice`
4. Call `render_brand_voice_md(doc)` → markdown string
5. Write to Secret Manager `brandvoice-content` secret as new version
6. Write to `_system/activity_log` that brand voice was pushed

**Response:** `{ "version": 5, "pushed_at": "2026-03-28T14:00:00Z" }`

---

### GET `/api/brand-voice/history`
Returns last 10 push events.

**Source:** `_system/brand_voice_history` — one doc per push, written by `POST /api/brand-voice`.

**Response:**
```json
{
  "history": [
    { "version": 5, "pushed_at": "2026-03-28T14:00:00Z", "sections_changed": ["identity", "lexicon"], "pushed_by": "dashboard" }
  ]
}
```

---

### POST `/api/brand-voice/preview`
Generates example replies using the submitted (unsaved) brand voice.

**Body:** Full brand voice document (not yet saved).

**Logic:** Call `claude-haiku-4-5-20251001` with brand voice as system prompt. Generate examples for: `nudge`, `dm`, `comment`, `peer`. Cache for 10 minutes keyed on hash of submitted doc.

**Response:**
```json
{
  "previews": {
    "nudge": "Glad it caught your eye 😏 What is it about this kind of thing that keeps pulling you back?",
    "dm": "That repost means a lot 🙌 There's a lot more on the other side — DMs are always open.",
    "comment": "This energy never misses.",
    "peer": "Always love what you put out. DMs open."
  }
}
```

---

## Markdown renderer

Implement `render_brand_voice_md(doc)` in `web/api/brand_voice.py`. Takes the Firestore document dict, produces a markdown string matching the `brandvoice/brandvoice-template-v1.md` schema structure.

---

## Bot integration

In `bluesky/shared/bluesky_client.py` or the brand voice loading path:

1. Try Firestore `_system/brand_voice` → if doc exists and `pushed_at` is newer than file mtime → use Firestore
2. Else → fall back to `BRANDVOICE_PATH` file
3. Convert to prompt string using `render_brand_voice_md()` before passing to Claude

---

## Tone review integration

When a tone review chat session produces an approved brand voice change:
1. Frontend calls `POST /api/brand-voice` with updated document
2. On success, mark the affected section dirty with a diff showing the change
3. Do not auto-push — user must review the diff and click "push to bot" deliberately

---

## Query bar context

The brand voice page query bar handles questions about the current brand voice document directly — no Firestore query needed. Questions like "what are my banned words" or "show me the DM pull rules" should be answered from the in-memory document already loaded on the page. The `POST /api/query` endpoint should detect brand-voice-related questions (via `context_page: "brand_voice"`) and read from `_system/brand_voice` rather than `engagement_events`.
