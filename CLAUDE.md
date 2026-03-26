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

**Local Firestore (testing):**
```bash
firebase emulators:start --only firestore
```
Set `FIRESTORE_EMULATOR_HOST=localhost:8080` in `.env` for local runs. Remove it for production.

---

## GCP / Firebase

- **Project:** `sx-platform`
- **Region:** `us-central1`
- **Firestore collections:** `replied_posts`, `blocked_users`, `bot_status`
- **Auth:** `gcloud auth login` as `sean@seanxavier.com`

---

## Notification Scope (extensibility)

Currently handles: `reply`

To add `mention` or creator-reply tracking: add a handler to the `HANDLERS` dict in `bluesky/reply/poller.py`. No refactor needed — the dispatch table is designed for this.

```python
HANDLERS = {
    "reply": handle_reply,        # active
    # "mention": handle_mention,  # add when ready
    # "quote": handle_quote,      # add when ready
}
```

---

## Environment Variables

See `bluesky/reply/.env.example` for the full list. Required:

```
BLUESKY_HANDLE=           # e.g. seanxavier.bsky.social
BLUESKY_APP_PASSWORD=     # from bsky.app → Settings → App Passwords
ANTHROPIC_API_KEY=
BRANDVOICE_PATH=          # absolute path to SX_Instructions.md
GOOGLE_CLOUD_PROJECT=sx-platform
```

Local only (remove for production):
```
FIRESTORE_EMULATOR_HOST=localhost:8080
```

---

## Module Map

| Path | Purpose |
|---|---|
| `brandvoice/brandvoice-template-v1.md` | BrandVoice schema (versioned) |
| `bluesky/shared/bluesky_client.py` | atproto wrapper — auth, fetch, post |
| `bluesky/reply/reply_generator.py` | Claude API — generates reply text |
| `bluesky/reply/state_manager.py` | Firestore — dedup, blocklist, bot status |
| `bluesky/reply/poller.py` | Main loop — poll → filter → generate → post |
| `bluesky/reply/admin.py` | CLI overrides |
| `bluesky/web/index.html` | Admin web UI (Phase 4) |

---

## Development Phases

| Phase | Status | Description |
|---|---|---|
| 0 | ✓ | Repo scaffold + BrandVoice template |
| 1 | — | Reply bot local (dry-run) |
| 2 | — | Tone iteration with real reply data |
| 3 | — | Live local testing |
| 4 | — | Firebase deploy + admin web UI |
| 5 | — | Scope expansion (mentions, creator replies, analytics) |
