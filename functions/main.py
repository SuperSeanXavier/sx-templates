"""
Cloud Function entry points — one handler per scheduled job.

All functions are HTTP-triggered by Cloud Scheduler (POST with empty body).
Secrets are mounted as environment variables via Secret Manager bindings at deploy time.

Environment variables required (set via --set-env-vars or --set-secrets at deploy):
  BLUESKY_HANDLE          e.g. seanxavier.bsky.social
  BLUESKY_APP_PASSWORD    from Secret Manager: projects/sx-platform/secrets/bluesky-app-password
  ANTHROPIC_API_KEY       from Secret Manager: projects/sx-platform/secrets/anthropic-api-key
  BRANDVOICE_CONTENT      from Secret Manager: projects/sx-platform/secrets/brandvoice-content
  GOOGLE_CLOUD_PROJECT    sx-platform
  FIRESTORE_DATABASE      sxplatformdatabase
  CREATOR_DETECTION_MUTUAL_FOLLOW  true

Discovery-only:
  DISCOVERY_CREATOR_HANDLE   e.g. seanxavier.bsky.social
  DISCOVERY_DOMAIN_KEYWORDS  comma-separated, e.g. "gay fitness,muscle,gay bodybuilder"
  DISCOVERY_DOMAINS          comma-separated domain tags, e.g. "fitness,muscle"
"""
import os
import sys
import time

import functions_framework
from dotenv import load_dotenv

# load_dotenv is a no-op when env vars are already set (Cloud Functions).
# In local dev, it reads from bluesky/reply/.env if present.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "bluesky", "reply", ".env"),
            override=False)

# Ensure project root is on the path so `bluesky.*` imports resolve.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _client():
    from bluesky.shared.bluesky_client import BlueskyClient
    return BlueskyClient().login()


def _brand_voice():
    from bluesky.reply.reply_generator import load_brand_voice
    return load_brand_voice()


def _log(fn_name, metrics, start, error=None):
    """Write a run record to Firestore. Called in every handler's finally block."""
    from bluesky.shared.activity_logger import log_run
    log_run(
        fn_name,
        metrics or {},
        status="error" if error else "ok",
        error_msg=str(error) if error else None,
        duration_s=time.time() - start,
    )


# ---------------------------------------------------------------------------
# poll-notifications   (every 5 min)
# ---------------------------------------------------------------------------

@functions_framework.http
def poll_notifications(request):
    """Run one notification cycle: replies, likes, reposts, follows."""
    from bluesky.reply.poller import run_once
    from bluesky.reply.state_manager import StateManager
    from bluesky.reply.dm_manager import DMManager

    start = time.time()
    metrics = {}
    err = None
    try:
        client = _client()
        state = StateManager()
        dm_state = DMManager()
        brand_voice = _brand_voice()
        metrics = run_once(client, state, brand_voice, dry_run=False, dm_state=dm_state) or {}
    except Exception as e:
        err = e
        raise
    finally:
        _log("poll-notifications", metrics, start, err)
    return ("OK", 200)


# ---------------------------------------------------------------------------
# poll-inbound-dms   (every 5 min)
# ---------------------------------------------------------------------------

@functions_framework.http
def poll_inbound_dms(request):
    """Check all active DM conversations for fan replies and respond."""
    from bluesky.engagement.fan_pipeline import poll_inbound_dms as _poll

    start = time.time()
    metrics = {}
    err = None
    try:
        client = _client()
        brand_voice = _brand_voice()
        metrics = _poll(client, brand_voice) or {}
    except Exception as e:
        err = e
        raise
    finally:
        _log("poll-inbound-dms", metrics, start, err)
    return ("OK", 200)


# ---------------------------------------------------------------------------
# scan-comment-targets   (every 15 min)
# ---------------------------------------------------------------------------

@functions_framework.http
def scan_comment_targets(request):
    """Fetch recent posts from Tier 1/2 accounts and queue qualifying ones."""
    from bluesky.engagement.comment_engine import scan_target_posts

    start = time.time()
    metrics = {}
    err = None
    try:
        client = _client()
        metrics = scan_target_posts(client) or {}
    except Exception as e:
        err = e
        raise
    finally:
        _log("scan-comment-targets", metrics, start, err)
    return ("OK", 200)


# ---------------------------------------------------------------------------
# execute-comment   (every 20 min, with in-process jitter)
# ---------------------------------------------------------------------------

@functions_framework.http
def execute_comment(request):
    """Dequeue and post one pending comment (0–10 min jitter built in)."""
    from bluesky.engagement.comment_engine import execute_comment_queue

    start = time.time()
    metrics = {}
    err = None
    try:
        client = _client()
        brand_voice = _brand_voice()
        metrics = execute_comment_queue(client, brand_voice) or {}
    except Exception as e:
        err = e
        raise
    finally:
        _log("execute-comment", metrics, start, err)
    return ("OK", 200)


# ---------------------------------------------------------------------------
# process-dm-queue   (every 2 hours)
# ---------------------------------------------------------------------------

@functions_framework.http
def process_dm_queue(request):
    """Pre-screen pending DM queue: mark already-DMed handles as skipped."""
    from bluesky.engagement.fan_pipeline import process_dm_queue_eligibility

    start = time.time()
    metrics = {}
    err = None
    try:
        metrics = process_dm_queue_eligibility() or {}
    except Exception as e:
        err = e
        raise
    finally:
        _log("process-dm-queue", metrics, start, err)
    return ("OK", 200)


# ---------------------------------------------------------------------------
# execute-dm-batch   (every 4 hours)
# ---------------------------------------------------------------------------

@functions_framework.http
def execute_dm_batch(request):
    """Dequeue up to 7 pending DMs, generate, and send with stagger.

    batch_size=7: with 8-min minimum stagger per send, 7 items fits within the
    3600s function timeout (7 × 480s = 3360s). Items not sent remain pending
    and are picked up on the next 4-hour invocation.

    Accepts optional JSON body: {"batch_size": N} to override the default.
    Pass batch_size=0 for a quick connectivity check (used by initiation_test.sh).
    """
    from bluesky.engagement.fan_pipeline import process_dm_queue as _process

    body = request.get_json(silent=True) or {}
    batch_size = int(body.get("batch_size", 7))

    start = time.time()
    metrics = {}
    err = None
    try:
        client = _client()
        brand_voice = _brand_voice()
        metrics = _process(client, brand_voice, batch_size=batch_size) or {}
    except Exception as e:
        err = e
        raise
    finally:
        _log("execute-dm-batch", metrics, start, err)
    return ("OK", 200)


# ---------------------------------------------------------------------------
# follower-graph-prefetch   (weekly Saturday 1am)
# ---------------------------------------------------------------------------

@functions_framework.http
def follower_graph_prefetch(request):
    """
    Phase A: fetch all fan profiles, filter to 1-std-dev band by followers_count,
    sort descending, store ordered DID list to Firestore for nightly slot runs.
    """
    from bluesky.engagement.discovery import prefetch_fan_profiles

    creator_handle = os.environ.get(
        "DISCOVERY_CREATOR_HANDLE",
        os.environ.get("BLUESKY_HANDLE", ""),
    )
    if not creator_handle:
        return ("DISCOVERY_CREATOR_HANDLE not set", 500)

    fan_cap = int(os.environ.get("FOLLOWER_GRAPH_FAN_CAP", "10000"))
    start = time.time()
    metrics = {}
    err = None
    try:
        client = _client()
        metrics = prefetch_fan_profiles(client, creator_handle, cap=fan_cap) or {}
    except Exception as e:
        err = e
        raise
    finally:
        _log("follower-graph-prefetch", metrics, start, err)
    return ("OK", 200)


# ---------------------------------------------------------------------------
# follower-graph-slot   (nightly, 5 jobs at 40-min intervals from 2am)
# ---------------------------------------------------------------------------

@functions_framework.http
def follower_graph_slot(request):
    """
    Phase B: process one slot of the pre-filtered fan list.
    Reads slot number from JSON request body: {"slot": 0}

    For each fan in the slot, fetches followees, sorts by their followers_count
    descending, takes the top FOLLOWER_GRAPH_TOP_PCT fraction, and
    frequency-counts those followees into target_accounts.
    """
    from bluesky.engagement.discovery import analyze_follower_graph_slot

    body = request.get_json(silent=True) or {}
    slot = int(body.get("slot", 0))

    creator_handle = os.environ.get(
        "DISCOVERY_CREATOR_HANDLE",
        os.environ.get("BLUESKY_HANDLE", ""),
    )
    if not creator_handle:
        return ("DISCOVERY_CREATOR_HANDLE not set", 500)

    slot_size = int(os.environ.get("FOLLOWER_GRAPH_SLOT_SIZE", "2000"))
    followee_cap = int(os.environ.get("FOLLOWER_GRAPH_FOLLOWEE_CAP", "500"))
    top_pct = float(os.environ.get("FOLLOWER_GRAPH_TOP_PCT", "0.20"))

    start = time.time()
    metrics = {}
    err = None
    try:
        client = _client()
        metrics = analyze_follower_graph_slot(
            client, creator_handle,
            slot=slot,
            slot_size=slot_size,
            followee_cap=followee_cap,
            top_pct=top_pct,
        ) or {}
    except Exception as e:
        err = e
        raise
    finally:
        _log("follower-graph-slot", metrics, start, err)
    return ("OK", 200)


# ---------------------------------------------------------------------------
# follower-graph-score   (daily 5:30am — after all 5 slots complete)
# ---------------------------------------------------------------------------

@functions_framework.http
def follower_graph_score(request):
    """
    Pure Firestore pass: re-tier all target_accounts based on discovery_sources
    and combined scores. Runs after the last nightly slot (slot 4 ends ~5:10am).
    """
    from bluesky.engagement.discovery import score_and_tier

    start = time.time()
    metrics = {}
    err = None
    try:
        metrics = score_and_tier() or {}
    except Exception as e:
        err = e
        raise
    finally:
        _log("follower-graph-score", metrics, start, err)
    return ("OK", 200)


# ---------------------------------------------------------------------------
# starter-pack-discovery   (weekly Sunday 3am)
# ---------------------------------------------------------------------------

@functions_framework.http
def starter_pack_discovery(request):
    """Search starter packs by domain keywords, score members, upsert target_accounts."""
    from bluesky.engagement.discovery import discover_starter_packs, score_and_tier

    raw_keywords = os.environ.get("DISCOVERY_DOMAIN_KEYWORDS", "")
    raw_domains = os.environ.get("DISCOVERY_DOMAINS", "")

    if not raw_keywords:
        return ("DISCOVERY_DOMAIN_KEYWORDS not set", 500)

    domain_keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()]
    domains = [d.strip() for d in raw_domains.split(",") if d.strip()]

    start = time.time()
    metrics = {}
    err = None
    try:
        client = _client()
        metrics = discover_starter_packs(client, domain_keywords, domains=domains) or {}
        tier_metrics = score_and_tier() or {}
        metrics.update(tier_metrics)
    except Exception as e:
        err = e
        raise
    finally:
        _log("starter-pack-discovery", metrics, start, err)
    return ("OK", 200)


# ---------------------------------------------------------------------------
# cleanup-stale-docs   (weekly Sunday 4am)
# ---------------------------------------------------------------------------

@functions_framework.http
def cleanup_stale_docs(request):
    """
    Delete old documents from unbounded collections to prevent indefinite growth.

    Retention policy:
      seen_events   — 7 days  (dedup window; anything older is harmless)
      dm_queue      — 30 days (sent/skipped records; pending items are untouched)
      comment_queue — 30 days (posted/skipped records; pending items are untouched)
    """
    from datetime import datetime, timezone, timedelta
    from google.cloud.firestore_v1.base_query import FieldFilter as _filter
    from bluesky.shared.firestore_client import db

    now = datetime.now(timezone.utc)
    cutoff_7d = (now - timedelta(days=7)).isoformat()
    cutoff_30d = (now - timedelta(days=30)).isoformat()
    cutoff_90d = (now - timedelta(days=90)).isoformat()

    start = time.time()
    del_seen = 0
    del_dm = 0
    del_comment = 0
    del_runs = 0
    err = None
    try:
        for doc in (
            db.collection("seen_events")
            .where(filter=_filter("seen_at", "<", cutoff_7d))
            .stream()
        ):
            doc.reference.delete()
            del_seen += 1

        for status in ("sent", "skipped"):
            for doc in (
                db.collection("dm_queue")
                .where(filter=_filter("status", "==", status))
                .where(filter=_filter("created_at", "<", cutoff_30d))
                .stream()
            ):
                doc.reference.delete()
                del_dm += 1

        for status in ("posted", "skipped"):
            for doc in (
                db.collection("comment_queue")
                .where(filter=_filter("status", "==", status))
                .where(filter=_filter("queued_at", "<", cutoff_30d))
                .stream()
            ):
                doc.reference.delete()
                del_comment += 1

        for doc in (
            db.collection("function_runs")
            .where(filter=_filter("run_at", "<", cutoff_90d))
            .stream()
        ):
            doc.reference.delete()
            del_runs += 1

        print(f"[cleanup] deleted {del_seen + del_dm + del_comment + del_runs} stale document(s)")
    except Exception as e:
        err = e
        raise
    finally:
        _log("cleanup-stale-docs", {
            "deleted_seen_events": del_seen,
            "deleted_dm_queue": del_dm,
            "deleted_comment_queue": del_comment,
            "deleted_function_runs": del_runs,
            "total_deleted": del_seen + del_dm + del_comment + del_runs,
        }, start, err)
    return ("OK", 200)
