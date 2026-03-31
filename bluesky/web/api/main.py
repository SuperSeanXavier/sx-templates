"""
FastAPI dashboard backend — SX Platform.

Auth: Firebase ID token (Google sign-in from dashboard) or DASHBOARD_SECRET (local dev/curl).
Local dev: uvicorn bluesky.web.api.main:app --reload --port 8000
            (run from project root so bluesky.* imports resolve)
"""
import hashlib
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_PT = ZoneInfo("America/Los_Angeles")
from typing import Any, Optional

import anthropic as _anthropic
import firebase_admin
import firebase_admin.auth as _fb_auth
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Ensure project root on path so bluesky.* imports resolve when running from
# bluesky/web/api/ or when the package is loaded directly.
_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Load .env (no-op in Cloud Run where env vars are already set)
load_dotenv(os.path.join(_ROOT, "bluesky", "reply", ".env"), override=False)

from bluesky.shared.cost_calculator import write_cost_event  # noqa: E402
from bluesky.shared.firestore_client import db  # noqa: E402
from bluesky.web.api.brand_voice import render_brand_voice_md  # noqa: E402
from google.cloud.firestore_v1.base_query import FieldFilter as _FF  # noqa: E402

# ---------------------------------------------------------------------------
# App + middleware
# ---------------------------------------------------------------------------

app = FastAPI(title="SX Platform Dashboard API", version="1.0.0")

_IS_PROD = bool(os.environ.get("K_SERVICE"))  # Cloud Run sets K_SERVICE

app.add_middleware(
    CORSMiddleware,
    allow_origins=(
        [os.getenv("DASHBOARD_ORIGIN", "https://sx-platform.web.app")]
        if _IS_PROD
        else ["null", "http://localhost:8000", "http://127.0.0.1:8000"]
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_security = HTTPBearer()
_AUTHORIZED_EMAILS = {"sean@seanxavier.com"}

# Initialise Firebase Admin once (uses ADC on Cloud Run, no extra config needed)
try:
    firebase_admin.initialize_app(options={
        "projectId": os.environ.get("GOOGLE_CLOUD_PROJECT", "sx-platform"),
    })
except ValueError:
    pass  # already initialised (e.g. during hot reload)


def _auth(credentials: HTTPAuthorizationCredentials = Depends(_security)) -> None:
    token = credentials.credentials
    # Try Firebase ID token first
    try:
        decoded = _fb_auth.verify_id_token(token)
        if decoded.get("email") not in _AUTHORIZED_EMAILS:
            raise HTTPException(status_code=403, detail="Forbidden")
        return
    except (_fb_auth.InvalidIdTokenError, _fb_auth.ExpiredIdTokenError,
            _fb_auth.RevokedIdTokenError, _fb_auth.CertificateFetchError,
            ValueError):
        pass
    except Exception:
        pass
    # Fallback: DASHBOARD_SECRET (local dev / curl)
    secret = os.getenv("DASHBOARD_SECRET", "")
    if not secret or token != secret:
        raise HTTPException(status_code=401, detail="Not authenticated")


_AUTH = Depends(_auth)

# ---------------------------------------------------------------------------
# Helpers — state.json
# ---------------------------------------------------------------------------


def _state_path() -> str:
    default = os.path.join(_ROOT, "bluesky", "reply", "state.json")
    return os.getenv("STATE_PATH", default)


def _read_state() -> dict:
    try:
        with open(_state_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(updates: dict) -> None:
    """Merge updates into state.json. Best-effort."""
    try:
        path = _state_path()
        try:
            with open(path) as f:
                state = json.load(f)
        except Exception:
            state = {}
        state.update(updates)
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers — settings
# ---------------------------------------------------------------------------


def _default_settings() -> dict:
    return {
        "bot": {
            "status": _read_state().get("bot_status", "running"),
            "poll_interval_seconds": 60,
            "max_conversation_depth": int(os.getenv("MAX_CONVERSATION_DEPTH", "3")),
            "reply_delay_mode": "90-600s",
        },
        "caps": {
            "max_discounts_per_day": int(os.getenv("MAX_DISCOUNTS_PER_DAY", "5")),
            "max_comments_per_day": int(os.getenv("DAILY_COMMENT_CAP", "50")),
            "max_dm_outreach_per_day": int(os.getenv("DAILY_DM_CAP", "50")),
            "monthly_spend_cap_usd": (
                float(os.getenv("MONTHLY_SPEND_CAP_USD"))
                if os.getenv("MONTHLY_SPEND_CAP_USD")
                else None
            ),
        },
        "creator_detection": {
            "mutual_follow": os.getenv("CREATOR_DETECTION_MUTUAL_FOLLOW", "true").lower() == "true",
            "bio_keywords": os.getenv("CREATOR_DETECTION_BIO", "false").lower() == "true",
            "follower_count": os.getenv("CREATOR_DETECTION_FOLLOWER_COUNT", "false").lower() == "true",
            "follower_threshold": int(os.getenv("CREATOR_FOLLOWER_THRESHOLD", "500")),
            "collab_dm_threshold": int(os.getenv("COLLAB_DM_THRESHOLD", "20000")),
        },
        "studio_handles": [h.strip() for h in os.getenv("STUDIO_HANDLES", "").split(",") if h.strip()],
        "themed_handles": [h.strip() for h in os.getenv("THEMED_HANDLES", "").split(",") if h.strip()],
        "discounts": {
            "fan_discount_code": os.getenv("FAN_DISCOUNT_CODE", ""),
            "fan_discount_url_reply": os.getenv("FAN_DISCOUNT_URL_REPLY", ""),
            "fan_discount_url_like": os.getenv("FAN_DISCOUNT_URL_LIKE", ""),
            "fan_discount_url_repost": os.getenv("FAN_DISCOUNT_URL_REPOST", ""),
        },
        "notifications": {
            "handoff_alerts": True,
            "rate_limit_alerts": True,
            "discount_cap_alerts": True,
            "spend_cap_alerts": False,
        },
    }


def _deep_merge(base: dict, overlay: dict) -> dict:
    result = dict(base)
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_settings() -> dict:
    defaults = _default_settings()
    try:
        doc = db.collection("_system").document("settings").get()
        if doc.exists:
            return _deep_merge(defaults, doc.to_dict() or {})
    except Exception:
        pass
    return defaults


# ---------------------------------------------------------------------------
# Helpers — time / bucketing
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ago_string(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        secs = int((_utc_now() - dt).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return ""


def _range_bounds(range_: str) -> tuple[datetime, datetime]:
    now = _utc_now()
    if range_ == "24h":
        return now - timedelta(hours=24), now
    if range_ == "7d":
        return now - timedelta(days=7), now
    if range_ == "30d":
        return now - timedelta(days=30), now
    return now - timedelta(days=7), now


def _range_buckets(range_: str) -> list[dict]:
    now = _utc_now()
    buckets = []
    if range_ == "24h":
        for i in range(23, -1, -1):
            start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=i)
            end = start + timedelta(hours=1)
            label = f"{start.astimezone(_PT).hour}h"
            buckets.append({"label": label, "start": start, "end": end})
    elif range_ == "7d":
        for i in range(6, -1, -1):
            day = (now - timedelta(days=i)).date()
            start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            end = start + timedelta(days=1)
            label = start.strftime("%a")
            buckets.append({"label": label, "start": start, "end": end})
    elif range_ == "30d":
        for i in range(4, -1, -1):
            ref = (now - timedelta(weeks=i)).date()
            week_start = ref - timedelta(days=ref.weekday())
            start = datetime(week_start.year, week_start.month, week_start.day, tzinfo=timezone.utc)
            end = start + timedelta(weeks=1)
            label = f"{start.strftime('%b')} {start.day}"
            buckets.append({"label": label, "start": start, "end": end})
    return buckets


def _in_bucket(events: list[dict], b: dict) -> list[dict]:
    bs = b["start"].isoformat()
    be = b["end"].isoformat()
    return [e for e in events if bs <= (e.get("created_at") or "") < be]


# ---------------------------------------------------------------------------
# Per-function health evaluation
# ---------------------------------------------------------------------------

# Strategies:
#   consecutive(n) — last N runs for this function are all errors (function is stuck)
#   any_today      — any error run today (every failure matters at this frequency)
#   any_occurrence — most recent run ever was an error (weekly/low-freq functions)
#   warning_only   — failures visible in drilldown but never counted in health-bar number
_FUNCTION_HEALTH_CONFIG: dict[str, dict] = {
    "poll-notifications":      {"strategy": "consecutive", "n": 6},  # 30 min stuck
    "poll-inbound-dms":        {"strategy": "consecutive", "n": 6},  # 30 min stuck
    "scan-comment-targets":    {"strategy": "consecutive", "n": 3},  # 45 min stuck
    "execute-comment":         {"strategy": "consecutive", "n": 3},  # 60 min stuck
    "process-dm-queue":        {"strategy": "any_today"},
    "execute-dm-batch":        {"strategy": "any_today"},
    "follower-graph-slot":     {"strategy": "any_today"},
    "follower-graph-score":    {"strategy": "any_today"},
    "snapshot-follower-count": {"strategy": "any_today"},
    "follower-graph-prefetch": {"strategy": "any_occurrence"},
    "starter-pack-discovery":  {"strategy": "any_occurrence"},
    "cleanup-stale-docs":      {"strategy": "warning_only"},
}


def _eval_fn_health(fn_name: str, cfg: dict, today: str) -> dict:
    """
    Evaluate one function's health against its configured strategy.
    Returns {"status": "ok"|"error"|"warning", "reason": str|None, "runs": list[dict]}.
    """
    strategy = cfg["strategy"]
    run_dicts: list[dict] = []
    status = "ok"
    reason = None

    try:
        _runs = db.collection("function_runs")

        if strategy == "consecutive":
            n = cfg["n"]
            docs = list(
                _runs.where(filter=_FF("function", "==", fn_name))
                .order_by("run_at", direction="DESCENDING")
                .limit(n)
                .stream()
            )
            run_dicts = [d.to_dict() for d in docs]
            if len(run_dicts) >= n and all(r.get("status") == "error" for r in run_dicts):
                status = "error"
                reason = f"Last {n} runs all failed"

        elif strategy == "any_today":
            docs = list(
                _runs.where(filter=_FF("function", "==", fn_name))
                .where(filter=_FF("date", "==", today))
                .order_by("run_at", direction="DESCENDING")
                .limit(50)
                .stream()
            )
            run_dicts = [d.to_dict() for d in docs]
            errors = [r for r in run_dicts if r.get("status") == "error"]
            if errors:
                status = "error"
                n_e = len(errors)
                reason = f"{n_e} failure{'s' if n_e != 1 else ''} today"

        elif strategy == "any_occurrence":
            docs = list(
                _runs.where(filter=_FF("function", "==", fn_name))
                .order_by("run_at", direction="DESCENDING")
                .limit(1)
                .stream()
            )
            run_dicts = [d.to_dict() for d in docs]
            if run_dicts and run_dicts[0].get("status") == "error":
                status = "error"
                reason = "Most recent run failed"

        elif strategy == "warning_only":
            docs = list(
                _runs.where(filter=_FF("function", "==", fn_name))
                .where(filter=_FF("date", "==", today))
                .order_by("run_at", direction="DESCENDING")
                .limit(10)
                .stream()
            )
            run_dicts = [d.to_dict() for d in docs]
            errors = [r for r in run_dicts if r.get("status") == "error"]
            if errors:
                status = "warning"
                n_e = len(errors)
                reason = f"{n_e} failure{'s' if n_e != 1 else ''} today"

    except Exception:
        pass

    return {"status": status, "reason": reason, "runs": run_dicts}


def _build_error_detail(fn_name: str, cfg: dict, eval_result: dict, now: datetime) -> dict:
    """Build the per-function detail object returned by GET /api/errors."""
    runs = eval_result["runs"]
    strategy = cfg["strategy"]
    n = cfg.get("n")
    strategy_label = f"consecutive({n})" if strategy == "consecutive" else strategy

    # Last successful run timestamp
    last_ok_at = None
    for r in runs:
        if r.get("status") == "ok":
            last_ok_at = r.get("run_at")
            break

    # If not found in recent runs, do a quick separate lookup (best effort)
    if last_ok_at is None and eval_result["status"] in ("error", "warning"):
        try:
            ok_docs = list(
                db.collection("function_runs")
                .where(filter=_FF("function", "==", fn_name))
                .where(filter=_FF("status", "==", "ok"))
                .order_by("run_at", direction="DESCENDING")
                .limit(1)
                .stream()
            )
            if ok_docs:
                last_ok_at = (ok_docs[0].to_dict() or {}).get("run_at")
        except Exception:
            pass

    # Human-readable last-ok age
    last_ok_ago = None
    if last_ok_at:
        try:
            dt = datetime.fromisoformat(last_ok_at.replace("Z", "+00:00"))
            secs = int((now - dt).total_seconds())
            if secs < 3600:
                last_ok_ago = f"{secs // 60} min ago"
            elif secs < 86400:
                last_ok_ago = f"{secs // 3600}h ago"
            else:
                last_ok_ago = f"{secs // 86400}d ago"
        except Exception:
            pass

    # Deduplicated error messages with counts
    from collections import Counter
    error_msgs = [r.get("error_msg") or "unknown error" for r in runs if r.get("status") == "error"]
    top_errors = [f"{msg} (×{cnt})" if cnt > 1 else msg
                  for msg, cnt in Counter(error_msgs).most_common(3)]

    today = now.date().isoformat()
    runs_today = [r for r in runs if r.get("date") == today]
    errors_today = [r for r in runs_today if r.get("status") == "error"]

    return {
        "function": fn_name,
        "status": eval_result["status"],
        "strategy": strategy_label,
        "reason": eval_result["reason"],
        "last_ok_at": last_ok_at,
        "last_ok_ago": last_ok_ago,
        "top_errors": top_errors,
        "runs_today": len(runs_today),
        "errors_today": len(errors_today),
        "warning_only": strategy == "warning_only",
    }


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


@app.get("/api/health", dependencies=[_AUTH])
def get_health():
    now = _utc_now()
    today = now.date().isoformat()

    # Most recent function run
    last_run_at = None
    last_run_ago = None
    try:
        runs = list(
            db.collection("function_runs")
            .order_by("run_at", direction="DESCENDING")
            .limit(1)
            .stream()
        )
        if runs:
            last_run_at = (runs[0].to_dict() or {}).get("run_at")
            if last_run_at:
                dt = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
                last_run_ago = int((now - dt).total_seconds())
    except Exception:
        pass

    # Per-function health evaluation — count functions worth attention
    error_count = 0
    has_warnings = False
    try:
        for fn_name, cfg in _FUNCTION_HEALTH_CONFIG.items():
            result = _eval_fn_health(fn_name, cfg, today)
            if result["status"] == "error":
                error_count += 1
            elif result["status"] == "warning":
                has_warnings = True
    except Exception:
        pass

    # Write window (4-min global window tracked in _system/rate_state)
    write_window = "clear"
    try:
        rate_doc = db.collection("_system").document("rate_state").get()
        if rate_doc.exists:
            last_write = (rate_doc.to_dict() or {}).get("last_write_at", "")
            if last_write:
                last_write_dt = datetime.fromisoformat(last_write.replace("Z", "+00:00"))
                secs_since = (now - last_write_dt).total_seconds()
                if secs_since < 240:
                    write_window = f"cooldown ({int(240 - secs_since)}s)"
    except Exception:
        pass

    # Bot status
    bot_status = "running"
    try:
        settings_doc = db.collection("_system").document("settings").get()
        if settings_doc.exists:
            bot_status = (settings_doc.to_dict() or {}).get("bot", {}).get("status", "running")
        else:
            bot_status = _read_state().get("bot_status", "running")
    except Exception:
        bot_status = _read_state().get("bot_status", "running")

    return {
        "bot_status": bot_status,
        "last_run_at": last_run_at,
        "last_run_ago_seconds": last_run_ago,
        "rate_limit_pct_used": 0,
        "write_window": write_window,
        "error_count_today": error_count,
        "has_warnings": has_warnings,
    }


# ---------------------------------------------------------------------------
# GET /api/errors
# ---------------------------------------------------------------------------


@app.get("/api/errors", dependencies=[_AUTH])
def get_errors():
    """Per-function health breakdown for the dashboard error drilldown panel."""
    now = _utc_now()
    today = now.date().isoformat()
    detail = []
    for fn_name, cfg in _FUNCTION_HEALTH_CONFIG.items():
        result = _eval_fn_health(fn_name, cfg, today)
        detail.append(_build_error_detail(fn_name, cfg, result, now))
    # Sort: errors first, then warnings, then ok
    order = {"error": 0, "warning": 1, "ok": 2}
    detail.sort(key=lambda d: order.get(d["status"], 3))
    return detail


# ---------------------------------------------------------------------------
# GET /api/caps
# ---------------------------------------------------------------------------


@app.get("/api/caps", dependencies=[_AUTH])
def get_caps():
    settings = _load_settings()
    caps = settings.get("caps", {})
    now_pt = datetime.now(_PT)
    today = now_pt.date().isoformat()
    # Start of today in PT expressed as UTC ISO string — used for timestamp comparisons
    today_pt_start = datetime(now_pt.year, now_pt.month, now_pt.day, tzinfo=_PT).astimezone(timezone.utc).isoformat()

    comments_today = 0
    dms_today = 0
    discounts_today = 0

    try:
        docs = (
            db.collection("comment_queue")
            .where(filter=_FF("status", "==", "posted"))
            .where(filter=_FF("posted_at", ">=", today))
            .stream()
        )
        comments_today = sum(1 for _ in docs)
    except Exception:
        pass

    try:
        docs = (
            db.collection("dm_queue")
            .where(filter=_FF("status", "==", "sent"))
            .where(filter=_FF("sent_at", ">=", today))
            .stream()
        )
        dms_today = sum(1 for _ in docs)
    except Exception:
        pass

    try:
        docs = (
            db.collection("conversations")
            .where(filter=_FF("discount_sent", "==", True))
            .where(filter=_FF("discount_sent_at", ">=", today_pt_start))
            .stream()
        )
        discounts_today = sum(1 for _ in docs)
    except Exception:
        pass

    return {
        "comments": {
            "cap": caps.get("max_comments_per_day", 50),
            "used": comments_today,
            "remaining": max(0, caps.get("max_comments_per_day", 50) - comments_today),
        },
        "dm_outreach": {
            "cap": caps.get("max_dm_outreach_per_day", 50),
            "used": dms_today,
            "remaining": max(0, caps.get("max_dm_outreach_per_day", 50) - dms_today),
        },
        "discounts": {
            "cap": None,
            "used": discounts_today,
            "remaining": None,
        },
    }


# ---------------------------------------------------------------------------
# GET /api/settings   POST /api/settings
# ---------------------------------------------------------------------------


@app.get("/api/settings", dependencies=[_AUTH])
def get_settings():
    return _load_settings()


@app.post("/api/settings", dependencies=[_AUTH])
async def post_settings(request: Request):
    body = await request.json()
    now_iso = _utc_now().isoformat()

    # Danger zone actions
    action = body.get("action")
    if action:
        return _handle_danger_zone(action, now_iso)

    current = _load_settings()
    merged = _deep_merge(current, body)

    # Compute changed fields
    changed_fields: list[str] = []

    def _diff(old: Any, new: Any, prefix: str = "") -> None:
        if isinstance(old, dict) and isinstance(new, dict):
            for k in set(list(old) + list(new)):
                _diff(old.get(k), new.get(k), f"{prefix}.{k}" if prefix else k)
        elif old != new:
            changed_fields.append(prefix)

    _diff(current, merged)

    db.collection("_system").document("settings").set(merged)

    # Handle bot.status change
    new_status = merged.get("bot", {}).get("status")
    if "bot.status" in changed_fields and new_status:
        _write_state({"bot_status": new_status})
        try:
            db.collection("function_runs").add({
                "function": "_dashboard",
                "run_at": now_iso,
                "date": _utc_now().date().isoformat(),
                "status": "ok",
                "error_msg": None,
                "duration_s": 0.0,
                "metrics": {"event": "bot_status_changed", "new_status": new_status},
            })
        except Exception:
            pass

    return {"updated_at": now_iso, "fields_changed": changed_fields}


def _handle_danger_zone(action: str, now_iso: str) -> dict:
    if action == "clear_dedup_state":
        count = 0
        try:
            for doc in db.collection("seen_events").limit(500).stream():
                doc.reference.delete()
                count += 1
        except Exception:
            pass
        _write_state({"replied_posts": {}, "my_reply_uris": {}})
        return {"action": action, "cleared": count, "at": now_iso}

    if action == "reset_user_classifications":
        count = 0
        try:
            for doc in db.collection("conversations").stream():
                doc.reference.update({"user_type": None, "classified_at": None})
                count += 1
        except Exception:
            pass
        return {"action": action, "reset": count, "at": now_iso}

    if action == "clear_dm_queue":
        count = 0
        try:
            for doc in db.collection("dm_queue").where(filter=_FF("status", "==", "pending")).stream():
                doc.reference.update({"status": "skipped", "skip_reason": "cleared_by_dashboard"})
                count += 1
        except Exception:
            pass
        return {"action": action, "cleared": count, "at": now_iso}

    if action == "clear_comment_queue":
        count = 0
        try:
            for doc in db.collection("comment_queue").where(filter=_FF("status", "==", "pending")).stream():
                doc.reference.update({"status": "skipped", "skip_reason": "cleared_by_dashboard"})
                count += 1
        except Exception:
            pass
        return {"action": action, "cleared": count, "at": now_iso}

    raise HTTPException(status_code=400, detail=f"Unknown action: {action}")


# ---------------------------------------------------------------------------
# GET /api/funnel
# ---------------------------------------------------------------------------


@app.get("/api/funnel", dependencies=[_AUTH])
def get_funnel(range: str = "7d"):
    start_dt, _ = _range_bounds(range)
    try:
        events = [
            d.to_dict()
            for d in db.collection("engagement_events")
            .where(filter=_FF("created_at", ">=", start_dt.isoformat()))
            .stream()
        ]
    except Exception:
        events = []

    _ENGAGED = {"engaged", "converted", "subscriber"}
    try:
        convos = [
            d.to_dict()
            for d in db.collection("conversations")
            .where(filter=_FF("created_at", ">=", start_dt.isoformat()))
            .stream()
        ]
    except Exception:
        convos = []

    # Effectiveness = discount sent. Bucket by discount_sent_at (new field) or
    # last_message_at (proxy for existing records without the timestamp).
    try:
        discounted = [
            d.to_dict()
            for d in db.collection("conversations")
            .where(filter=_FF("discount_sent", "==", True))
            .stream()
        ]
    except Exception:
        discounted = []

    # Normalise: give each discounted convo a single timestamp for bucketing
    for c in discounted:
        c["_eff_at"] = c.get("discount_sent_at") or c.get("last_message_at") or c.get("created_at")

    result = []
    for b in _range_buckets(range):
        bucket_events = _in_bucket(events, b)
        bucket_convos = _in_bucket(convos, b)
        bs, be = b["start"].isoformat(), b["end"].isoformat()
        bucket_discounted = [c for c in discounted if bs <= (c.get("_eff_at") or "") < be]
        dms_sent = sum(1 for e in bucket_events if e.get("type") == "dm" and e.get("direction") == "outbound")
        posts = sum(1 for e in bucket_events if e.get("type") == "post")
        engagement = sum(1 for c in bucket_convos if c.get("stage") in _ENGAGED)
        effectiveness = len(bucket_discounted)

        result.append({
            "label": b["label"],
            "timestamp": b["start"].isoformat(),
            "dms_sent": dms_sent,
            "engagement": engagement,
            "effectiveness": effectiveness,
            "posts": posts,
        })

    return {"range": range, "buckets": result}


# ---------------------------------------------------------------------------
# GET /api/funnel/snapshot
# ---------------------------------------------------------------------------


@app.get("/api/funnel/snapshot", dependencies=[_AUTH])
def get_funnel_snapshot(period: str = "Mon", range: str = "7d"):
    bucket = next((b for b in _range_buckets(range) if b["label"] == period), None)
    if not bucket:
        return {"period": period, "fan_replies": 0, "nudge_sent": 0, "intent_signal": 0, "dm_pull": 0}

    try:
        events = [
            d.to_dict()
            for d in db.collection("engagement_events")
            .where(filter=_FF("created_at", ">=", bucket["start"].isoformat()))
            .where(filter=_FF("created_at", "<", bucket["end"].isoformat()))
            .stream()
        ]
    except Exception:
        events = []

    return {
        "period": period,
        "fan_replies": sum(1 for e in events if e.get("type") == "reply" and e.get("direction") == "outbound"),
        "nudge_sent": sum(1 for e in events if e.get("reply_type") == "nudge"),
        "intent_signal": sum(1 for e in events if e.get("direction") == "inbound" and e.get("fan_intent") in ("buying_signal", "curious")),
        "dm_pull": sum(1 for e in events if e.get("reply_type") == "dm_pull"),
    }


# ---------------------------------------------------------------------------
# GET /api/growth
# ---------------------------------------------------------------------------


@app.get("/api/growth", dependencies=[_AUTH])
def get_growth(range: str = "7d"):
    buckets = _range_buckets(range)
    follower_trend = []

    for b in buckets:
        date_str = b["start"].date().isoformat()
        count = 0
        try:
            doc = (
                db.collection("_system")
                .document("follower_snapshots")
                .collection("daily")
                .document(date_str)
                .get()
            )
            if doc.exists:
                count = (doc.to_dict() or {}).get("count", 0)
        except Exception:
            pass
        follower_trend.append({"label": b["label"], "total": count, "new": 0})

    prev_total = None
    for item in follower_trend:
        if prev_total is not None:
            item["new"] = max(0, item["total"] - prev_total)
        prev_total = item["total"]

    total_followers = follower_trend[-1]["total"] if follower_trend else 0
    new_today = follower_trend[-1]["new"] if follower_trend else 0
    weekly_gain = sum(b["new"] for b in follower_trend)
    start_total = follower_trend[0]["total"] if follower_trend else 0
    weekly_growth_pct = round((weekly_gain / start_total * 100), 1) if start_total else 0.0

    breakdown = {"fan": 0, "creator": 0, "themed": 0, "studio": 0, "likely_bot": 0}
    try:
        for doc in db.collection("conversations").stream():
            ut = (doc.to_dict() or {}).get("user_type") or "fan"
            breakdown[ut] = breakdown.get(ut, 0) + 1
    except Exception:
        pass

    discovery = {"tier_1": 0, "tier_2": 0, "tier_3": 0, "total": 0}
    try:
        week_ago = (_utc_now() - timedelta(days=7)).isoformat()
        for doc in db.collection("target_accounts").where(filter=_FF("created_at", ">=", week_ago)).stream():
            tier = (doc.to_dict() or {}).get("tier", 3)
            key = f"tier_{tier}" if tier in (1, 2) else "tier_3"
            discovery[key] = discovery.get(key, 0) + 1
            discovery["total"] += 1
    except Exception:
        pass

    return {
        "range": range,
        "labels": [b["label"] for b in follower_trend],
        "total": [b["total"] for b in follower_trend],
        "daily": [b["new"] for b in follower_trend],
        "total_followers": total_followers,
        "new_today": new_today,
        "weekly_gain": weekly_gain,
        "weekly_growth_pct": weekly_growth_pct,
        "follower_type_breakdown": breakdown,
        "discovery_this_week": discovery,
    }


# ---------------------------------------------------------------------------
# GET /api/audience
# ---------------------------------------------------------------------------


@app.get("/api/audience", dependencies=[_AUTH])
def get_audience(
    sort: str = "score",
    search: str = "",
    tier: int = 0,
    limit: int = 100,
    offset: int = 0,
):
    query = db.collection("target_accounts")
    if tier in (1, 2, 3):
        query = query.where(filter=_FF("tier", "==", tier))

    docs = [d.to_dict() for d in query.stream()]

    if search:
        s = search.lower().lstrip("@")
        docs = [d for d in docs if s in (d.get("handle") or "").lower() or s in (d.get("display_name") or "").lower()]

    sort_key = {
        "score": lambda d: d.get("follower_graph_score") or 0,
        "followers": lambda d: d.get("follower_count") or 0,
        "overlap": lambda d: d.get("follower_graph_count") or 0,
    }.get(sort, lambda d: d.get("follower_graph_score") or 0)
    docs.sort(key=sort_key, reverse=True)

    # Score distribution buckets: 0-2, 2-4, 4-6, 6-8, 8-10
    distribution = [0, 0, 0, 0, 0]
    source_counts = {"follower_graph": 0, "starter_pack": 0, "both": 0}
    all_docs = [d.to_dict() for d in db.collection("target_accounts").stream()] if (search or tier) else docs
    for d in (all_docs if (search or tier) else docs):
        sc = d.get("follower_graph_score") or 0
        distribution[min(int(sc / 2), 4)] += 1
        srcs = set(d.get("discovery_sources") or [])
        if "follower_graph" in srcs and "starter_pack" in srcs:
            source_counts["both"] += 1
        elif "follower_graph" in srcs:
            source_counts["follower_graph"] += 1
        elif "starter_pack" in srcs:
            source_counts["starter_pack"] += 1

    total = len(docs)
    page = docs[offset: offset + limit]

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "sort": sort,
        "accounts": [
            {
                "handle": d.get("handle", ""),
                "display_name": d.get("display_name", ""),
                "follower_count": d.get("follower_count") or 0,
                "follower_graph_score": d.get("follower_graph_score") or 0,
                "follower_graph_count": d.get("follower_graph_count") or 0,
                "follower_graph_checked": d.get("follower_graph_checked") or 0,
                "tier": d.get("tier") or 3,
                "discovery_sources": d.get("discovery_sources") or [],
                "bio": (d.get("bio") or "")[:120],
            }
            for d in page
        ],
        "distribution": distribution,
        "source_counts": source_counts,
    }


# ---------------------------------------------------------------------------
# GET /api/audience/chart
# ---------------------------------------------------------------------------


@app.get("/api/audience/chart", dependencies=[_AUTH])
def get_audience_chart():
    """All accounts with minimal fields for scatter/quadrant charts."""
    docs = [d.to_dict() for d in db.collection("target_accounts").stream()]
    return {
        "points": [
            {
                "handle": d.get("handle", ""),
                "display_name": d.get("display_name", ""),
                "x": d.get("follower_count") or 0,
                "y": d.get("follower_graph_score") or 0,
                "tier": d.get("tier") or 3,
                "overlap": d.get("follower_graph_count") or 0,
            }
            for d in docs
            if (d.get("follower_count") or 0) > 0 and (d.get("follower_graph_score") or 0) > 0
        ]
    }


# ---------------------------------------------------------------------------
# GET /api/dm-effectiveness
# ---------------------------------------------------------------------------


@app.get("/api/dm-effectiveness", dependencies=[_AUTH])
def get_dm_effectiveness(range: str = "7d", period: Optional[str] = None):
    start_dt, _ = _range_bounds(range)
    start_iso = start_dt.isoformat()

    try:
        convos = [
            d.to_dict()
            for d in db.collection("conversations")
            .where(filter=_FF("last_message_at", ">=", start_iso))
            .stream()
        ]
    except Exception:
        convos = []

    # Discounts may have been sent on conversations created before the range window.
    # Load all discount_sent=True separately and filter by when the discount was sent.
    try:
        discounted_all = [
            d.to_dict()
            for d in db.collection("conversations")
            .where(filter=_FF("discount_sent", "==", True))
            .stream()
        ]
    except Exception:
        discounted_all = []

    # Use discount_sent_at if available, else last_message_at, else created_at
    discounted_in_range = {
        c.get("fan_handle") or c.get("trigger_context", "")
        for c in discounted_all
        if (c.get("discount_sent_at") or c.get("last_message_at") or c.get("created_at") or "") >= start_iso
    }

    RESPONDED = {"engaged", "converted", "subscriber"}

    total_sent = len(convos)
    total_responded = sum(1 for c in convos if c.get("stage") in RESPONDED)
    total_discounted = len(discounted_in_range)
    effectiveness = round(total_discounted / total_sent * 100) if total_sent else 0

    by_type: dict = {}
    for convo in convos:
        trigger = convo.get("trigger_context", "other")
        subtype = f"{trigger}_trigger" if trigger in ("like", "repost", "follow") else trigger
        if subtype not in by_type:
            by_type[subtype] = {"sent": 0, "responded": 0, "discounted": 0}
        by_type[subtype]["sent"] += 1
        if convo.get("stage") in RESPONDED:
            by_type[subtype]["responded"] += 1

    # Attribute discounts to trigger type using the same handle key
    for c in discounted_all:
        eff_at = c.get("discount_sent_at") or c.get("last_message_at") or c.get("created_at") or ""
        if eff_at < start_iso:
            continue
        trigger = c.get("trigger_context", "other")
        subtype = f"{trigger}_trigger" if trigger in ("like", "repost", "follow") else trigger
        if subtype in by_type:
            by_type[subtype]["discounted"] += 1

    return {
        "range": range,
        "period": period,
        "total_dms_sent": total_sent,
        "total_responded": total_responded,
        "total_discounted": total_discounted,
        "effectiveness_pct": effectiveness,
        "by_type": by_type,
    }


# ---------------------------------------------------------------------------
# GET /api/heatmap
# ---------------------------------------------------------------------------


@app.get("/api/heatmap", dependencies=[_AUTH])
def get_heatmap(mode: str = "replies", tz_offset: int = 0):
    # tz_offset = minutes west of UTC (JS getTimezoneOffset): PDT=420, EST=300
    local_offset = timedelta(minutes=-tz_offset)
    now_local = _utc_now() + local_offset
    start_local = (now_local - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    grid = [[0] * 24 for _ in range(7)]

    # Query with UTC equivalent of local window start
    start_utc = start_local - local_offset
    try:
        events = [
            d.to_dict()
            for d in db.collection("engagement_events")
            .where(filter=_FF("created_at", ">=", start_utc.isoformat()))
            .stream()
        ]
    except Exception:
        events = []

    for e in events:
        if mode == "dm_engagement" and not (e.get("type") == "dm" and e.get("direction") == "inbound"):
            continue
        if mode == "post_engagement" and not (e.get("direction") == "inbound" and e.get("type") in ("like", "repost", "reply", "engagement_queued")):
            continue
        try:
            dt_utc = datetime.fromisoformat(e["created_at"].replace("Z", "+00:00"))
            dt_local = dt_utc + local_offset
            day_idx = (dt_local.date() - start_local.date()).days
            if 0 <= day_idx < 7:
                grid[day_idx][dt_local.hour] += 1
        except Exception:
            pass

    days = [(start_local + timedelta(days=i)).strftime("%a") for i in range(7)]
    return {"mode": mode, "days": days, "hours": list(range(24)), "grid": grid}


# ---------------------------------------------------------------------------
# GET /api/handoff
# ---------------------------------------------------------------------------


_PAYMENT_KEYWORDS = {"paid", "payment", "charge", "refund", "billing", "money"}


@app.get("/api/handoff", dependencies=[_AUTH])
def get_handoff(limit: int = 10):
    items = []
    now = _utc_now()

    try:
        for doc in db.collection("conversations").where(filter=_FF("human_handoff", "==", True)).stream():
            data = doc.to_dict() or {}
            handle = data.get("handle", doc.id)
            waiting_since = data.get("last_message_at") or now.isoformat()
            try:
                waiting_dt = datetime.fromisoformat(waiting_since.replace("Z", "+00:00"))
                waiting_minutes = int((now - waiting_dt).total_seconds() / 60)
            except Exception:
                waiting_minutes = 0

            preview = data.get("last_fan_message", "")
            reason = data.get("handoff_reason", "flagged for review")
            has_payment = any(w in (preview + reason).lower() for w in _PAYMENT_KEYWORDS)
            urgency = "high" if (has_payment or waiting_minutes > 60) else ("med" if waiting_minutes > 120 else "low")
            initials = "".join(p[0].upper() for p in handle.lstrip("@").split(".")[:2]) or "??"

            items.append({
                "handle": f"@{handle.lstrip('@')}",
                "initials": initials,
                "preview": preview[:80],
                "reason": reason,
                "urgency": urgency,
                "waiting_since": waiting_since,
                "waiting_minutes": waiting_minutes,
                "convo_id": doc.id,
            })
    except Exception:
        pass

    items.sort(key=lambda x: ({"high": 0, "med": 1, "low": 2}[x["urgency"]], -x["waiting_minutes"]))
    return {"count": len(items), "items": items[:limit]}


# ---------------------------------------------------------------------------
# GET /api/handoff/{handle}   POST /api/handoff/{handle}/resolve
# ---------------------------------------------------------------------------


@app.get("/api/handoff/{handle}", dependencies=[_AUTH])
def get_handoff_detail(handle: str):
    handle = handle.lstrip("@")
    now = _utc_now()

    doc_ref = db.collection("conversations").document(handle)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Conversation not found")

    data = doc.to_dict() or {}
    waiting_since = data.get("last_message_at") or now.isoformat()
    try:
        waiting_dt = datetime.fromisoformat(waiting_since.replace("Z", "+00:00"))
        waiting_minutes = int((now - waiting_dt).total_seconds() / 60)
    except Exception:
        waiting_minutes = 0

    messages = []
    try:
        messages = [
            d.to_dict()
            for d in doc_ref.collection("messages").order_by("timestamp").stream()
        ]
    except Exception:
        pass

    preview = data.get("last_fan_message", "")
    reason = data.get("handoff_reason", "flagged for review")
    has_payment = any(w in (preview + reason).lower() for w in _PAYMENT_KEYWORDS)
    urgency = "high" if (has_payment or waiting_minutes > 60) else ("med" if waiting_minutes > 120 else "low")

    thread = [
        {"role": m.get("role", ""), "content": m.get("content", "")}
        for m in messages
        if m.get("content") and m.get("role") in ("user", "assistant", "human_operator")
    ]

    return {
        "handle": f"@{handle}",
        "reason": reason,
        "urgency": urgency,
        "waiting_minutes": waiting_minutes,
        "thread": thread,
        "human_handoff": data.get("human_handoff", True),
        "has_convo_id": bool(data.get("convo_id")),
    }


@app.post("/api/handoff/{handle}/resolve", dependencies=[_AUTH])
async def post_handoff_resolve(handle: str, request: Request):
    body = await request.json()
    reply_text = (body.get("reply_text") or "").strip()
    resume_automated = bool(body.get("resume_automated", True))
    remove_from_queue = bool(body.get("remove_from_queue", True))
    handle = handle.lstrip("@")
    now = _utc_now().isoformat()

    doc_ref = db.collection("conversations").document(handle)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Conversation not found")

    update: dict = {}
    if reply_text:
        update["pending_manual_reply"] = reply_text
        update["has_pending_manual_reply"] = True
        try:
            doc_ref.collection("messages").add({
                "role": "human_operator",
                "content": reply_text,
                "timestamp": now,
            })
        except Exception:
            pass

    if remove_from_queue:
        update["human_handoff"] = False
        update["handoff_reason"] = None
        if not resume_automated:
            # Removed from queue but bot stays off — add to paused_users in state.json
            try:
                state = _read_state()
                paused = state.get("paused_users", [])
                if handle not in paused and f"@{handle}" not in paused:
                    paused.append(handle)
                _write_state({"paused_users": paused})
            except Exception:
                pass
    # keep_in_queue (remove_from_queue=False): human_handoff stays True, no state change needed

    if update:
        doc_ref.update(update)

    return {
        "handle": f"@{handle}",
        "resolved_at": now,
        "reply_queued": bool(reply_text),
        "automated_resumed": resume_automated and remove_from_queue,
        "removed_from_queue": remove_from_queue,
    }


# ---------------------------------------------------------------------------
# GET /api/tone-review   POST /api/tone-review/{id}/feedback
# POST /api/tone-review/refresh
# ---------------------------------------------------------------------------


@app.get("/api/tone-review", dependencies=[_AUTH])
def get_tone_review(
    vehicle: Optional[str] = None,
    interaction: Optional[str] = None,
    surface: Optional[str] = None,
    limit: int = 10,
):
    all_items = []
    by_vehicle: dict = {}

    try:
        queue_doc = db.collection("_system").document("tone_review_queue").get()
        if queue_doc.exists:
            for item in (queue_doc.to_dict() or {}).get("items", []):
                v = item.get("vehicle")
                itype = item.get("interaction_type")
                if v not in by_vehicle:
                    by_vehicle[v] = {"all": 0}
                by_vehicle[v]["all"] += 1
                if itype:
                    by_vehicle[v][itype] = by_vehicle[v].get(itype, 0) + 1
                if vehicle and v != vehicle:
                    continue
                if interaction and itype != interaction:
                    continue
                if surface and item.get("surface_reason") != surface:
                    continue
                all_items.append(item)
    except Exception:
        pass

    session_history = {"last_session_days_ago": None, "approved": 0, "flagged": 0, "bv_updates": 0}
    try:
        fb_doc = db.collection("_system").document("tone_review_feedback").get()
        if fb_doc.exists:
            fb = fb_doc.to_dict() or {}
            session_history["approved"] = fb.get("approved_total", 0)
            session_history["flagged"] = fb.get("flagged_total", 0)
            last_at = fb.get("last_session_at")
            if last_at:
                last_dt = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
                session_history["last_session_days_ago"] = (_utc_now() - last_dt).days
    except Exception:
        pass

    return {
        "total": len(all_items),
        "by_vehicle": by_vehicle,
        "session_history": session_history,
        "items": all_items[:limit],
    }


@app.post("/api/tone-review/{item_id}/feedback", dependencies=[_AUTH])
async def post_tone_feedback(item_id: str, request: Request):
    body = await request.json()
    action = body.get("action")
    if action not in ("approve", "flag"):
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'flag'")

    now_iso = _utc_now().isoformat()
    try:
        fb_ref = db.collection("_system").document("tone_review_feedback")
        fb_doc = fb_ref.get()
        fb = fb_doc.to_dict() or {} if fb_doc.exists else {}
        fb["approved_total" if action == "approve" else "flagged_total"] = (
            fb.get("approved_total" if action == "approve" else "flagged_total", 0) + 1
        )
        fb["last_session_at"] = now_iso
        fb_ref.set(fb)
        record: dict = {"item_id": item_id, "action": action, "at": now_iso}
        for field in ("approved_text", "fan_message", "vehicle", "interaction_type", "fan_intent"):
            val = body.get(field)
            if val:
                record[field] = val
        fb_ref.collection("records").add(record)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"item_id": item_id, "action": action, "recorded_at": now_iso}


@app.post("/api/tone-review/{item_id}/discuss", dependencies=[_AUTH])
async def post_tone_discuss(item_id: str, request: Request):
    body = await request.json()
    message = body.get("message", "").strip()
    history = body.get("history", [])   # [{role, content}]
    item = body.get("item", {})

    if not message:
        raise HTTPException(status_code=400, detail="message required")

    # Load brand voice
    bv_md = ""
    try:
        bv_doc = db.collection("_system").document("brand_voice").get()
        if bv_doc.exists:
            bv_md = render_brand_voice_md(bv_doc.to_dict() or {})
    except Exception:
        pass
    if not bv_md:
        path = os.getenv("BRANDVOICE_PATH")
        if path:
            try:
                with open(path) as f:
                    bv_md = f.read()
            except Exception:
                pass

    vehicle = item.get("vehicle", "reply")
    interaction = item.get("interaction_type") or item.get("interaction", "")
    handle = item.get("handle", "unknown")
    fan_msg = item.get("fan_message") or item.get("fan", "")
    bot_reply = item.get("bot_reply") or item.get("bot", "")
    cls = item.get("classification") or {}
    surface = item.get("surface_reason") or item.get("surface", "sample")

    system_prompt = f"""You are a brand voice coach helping a creator review bot-generated messages.

Brand voice:
{bv_md}

Item under review:
- Vehicle: {vehicle}
- Interaction type: {interaction}
- Handle: {handle}
- Surface reason: {surface}
- Fan message/action: {fan_msg}
- Bot reply: {bot_reply}
- Post type: {cls.get("post_type", "—")}
- Fan intent: {cls.get("fan_intent", "—")}
- Mirror tier: {cls.get("mirror_tier", "—")}

Assess the tone concisely. If the creator flags an issue, provide 1-2 alternative draft replies.
Respond in JSON only: {{"reply": "your assessment", "drafts": ["draft1"]}}
drafts may be an empty array."""

    messages = [{"role": m["role"], "content": m["content"]} for m in history if m.get("role") in ("user", "assistant")]
    messages.append({"role": "user", "content": message})

    try:
        client = _anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system_prompt,
            messages=messages,
        )
        write_cost_event(db, resp.model, resp.usage, "tone_discuss")
        raw = resp.content[0].text.strip().strip("`").removeprefix("json").strip()
        try:
            parsed = json.loads(raw)
            reply_text = parsed.get("reply", raw)
            drafts = parsed.get("drafts", [])
        except Exception:
            reply_text = raw
            drafts = []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"reply": reply_text, "drafts": drafts}


@app.get("/api/tone-review/approved-examples", dependencies=[_AUTH])
def get_approved_examples_endpoint():
    try:
        docs = list(
            db.collection("_system").document("tone_review_feedback")
            .collection("records")
            .order_by("at", direction="DESCENDING")
            .limit(200)
            .stream()
        )
    except Exception:
        docs = []
    examples = []
    for d in docs:
        r = d.to_dict() or {}
        if r.get("action") == "approve" and r.get("approved_text") and r.get("fan_message"):
            examples.append({
                "id": d.id,
                "vehicle": r.get("vehicle", ""),
                "interaction_type": r.get("interaction_type", ""),
                "fan_intent": r.get("fan_intent"),
                "fan_message": r.get("fan_message", ""),
                "approved_text": r.get("approved_text", ""),
                "at": r.get("at", ""),
            })
    return {"examples": examples}


@app.patch("/api/tone-review/approved-examples/{record_id}", dependencies=[_AUTH])
async def patch_approved_example(record_id: str, request: Request):
    body = await request.json()
    approved_text = body.get("approved_text", "").strip()
    if not approved_text:
        raise HTTPException(status_code=400, detail="approved_text required")
    ref = (
        db.collection("_system")
        .document("tone_review_feedback")
        .collection("records")
        .document(record_id)
    )
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Record not found")
    ref.update({"approved_text": approved_text})
    return {"id": record_id, "approved_text": approved_text}


@app.delete("/api/tone-review/approved-examples/{record_id}", dependencies=[_AUTH])
def delete_approved_example(record_id: str):
    ref = (
        db.collection("_system")
        .document("tone_review_feedback")
        .collection("records")
        .document(record_id)
    )
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Record not found")
    ref.delete()
    return {"id": record_id, "deleted": True}


@app.post("/api/tone-review/refresh", dependencies=[_AUTH])
def post_tone_refresh():
    now = _utc_now()
    week_ago = (now - timedelta(days=7)).isoformat()
    items = []
    seen_combos: set = set()

    # --- Pass 1: conversations messages subcollection (primary source) ---
    try:
        convo_docs = list(
            db.collection("conversations")
            .where(filter=_FF("last_message_at", ">=", week_ago))
            .limit(100)
            .stream()
        )
    except Exception as e:
        print(f"[tone_refresh] conversations query error: {e}")
        convo_docs = []

    # Sort most-recently-active first
    convo_docs = sorted(
        convo_docs,
        key=lambda d: (d.to_dict() or {}).get("last_message_at", ""),
        reverse=True,
    )

    for cdoc in convo_docs:
        if len(items) >= 50:
            break
        cdata = cdoc.to_dict() or {}
        handle = cdata.get("fan_handle") or cdoc.id
        trigger = cdata.get("trigger_context", "other")

        try:
            msg_docs = list(
                db.collection("conversations").document(cdoc.id)
                .collection("messages")
                .stream()
            )
        except Exception:
            continue

        msgs = sorted(
            [m.to_dict() for m in msg_docs if m.to_dict()],
            key=lambda m: m.get("timestamp", ""),
        )

        # Find the most recent user → assistant pair
        pair = None
        for i in range(len(msgs) - 1, 0, -1):
            if msgs[i].get("role") == "assistant" and msgs[i - 1].get("role") == "user":
                pair = (msgs[i - 1].get("content", ""), msgs[i].get("content", ""))
                break

        if not pair or not pair[1]:
            continue

        fan_msg, bot_reply = pair
        subtype = f"{trigger}_trigger" if trigger in ("like", "repost", "follow") else trigger
        combo = ("DM", subtype)
        is_edge = combo not in seen_combos
        seen_combos.add(combo)

        items.append({
            "id": cdoc.id,
            "vehicle": "DM",
            "interaction_type": subtype,
            "surface_reason": "edge_case" if is_edge else "sample",
            "handle": f"@{handle.lstrip('@')}",
            "fan_message": fan_msg,
            "bot_reply": bot_reply,
            "classification": {
                "post_type": None,
                "fan_intent": cdata.get("stage"),
                "mirror_tier": None,
                "interaction": trigger,
            },
            "created_at": cdata.get("last_message_at"),
        })

    # --- Pass 2: engagement_events with bot_reply (public replies, future DM events) ---
    try:
        reply_docs = list(
            db.collection("engagement_events")
            .where(filter=_FF("created_at", ">=", week_ago))
            .limit(500)
            .stream()
        )
        reply_events = sorted(
            [{"id": d.id, **d.to_dict()} for d in reply_docs],
            key=lambda e: e.get("created_at", ""),
            reverse=True,
        )
    except Exception as e:
        print(f"[tone_refresh] engagement_events query error: {e}")
        reply_events = []

    for e in reply_events:
        if len(items) >= 50:
            break
        reply_type = e.get("reply_type")
        vehicle = _event_vehicle(e)
        if not vehicle or not reply_type or not e.get("bot_reply"):
            continue
        combo = (vehicle, reply_type)
        is_edge = combo not in seen_combos
        seen_combos.add(combo)
        items.append({
            "id": e["id"],
            "vehicle": vehicle,
            "interaction_type": reply_type,
            "surface_reason": "edge_case" if is_edge else "sample",
            "handle": f"@{e.get('handle', '').lstrip('@')}",
            "fan_message": e.get("fan_message", ""),
            "bot_reply": e.get("bot_reply", ""),
            "classification": {
                "post_type": e.get("post_type_classification"),
                "fan_intent": e.get("fan_intent"),
                "mirror_tier": e.get("mirror_tier"),
                "interaction": e.get("interaction_subtype"),
            },
            "created_at": e.get("created_at"),
        })

    try:
        db.collection("_system").document("tone_review_queue").set({
            "items": items,
            "refreshed_at": now.isoformat(),
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"refreshed": len(items), "at": now.isoformat()}


def _event_vehicle(e: dict) -> Optional[str]:
    t = e.get("type", "")
    if t == "reply":
        return "reply"
    if t == "dm":
        return "dm"
    if t == "comment" or e.get("reply_type") == "comment":
        return "comment"
    return None


# ---------------------------------------------------------------------------
# GET /api/activity   GET /api/activity/paused
# POST /api/activity/resume/{handle}
# ---------------------------------------------------------------------------


@app.get("/api/activity", dependencies=[_AUTH])
def get_activity(
    range: str = "24h",
    type: str = "all",
    limit: int = 50,
    handle: Optional[str] = None,
):
    start_dt, _ = _range_bounds(range)
    items = []

    try:
        query = db.collection("engagement_events").where(filter=_FF("created_at", ">=", start_dt.isoformat()))
        if handle:
            query = query.where(filter=_FF("handle", "==", handle.lstrip("@")))

        for doc in query.order_by("created_at", direction="DESCENDING").limit(limit).stream():
            e = doc.to_dict() or {}
            etype = e.get("type", "")
            subtype = e.get("reply_type") or e.get("interaction_subtype") or ""

            if type != "all":
                if type in ("reply", "dm", "comment") and etype != type:
                    continue
                if type == "flag" and e.get("interaction_subtype") != "handoff":
                    continue
                if type == "discount" and e.get("interaction_subtype") != "discount_sent":
                    continue

            h = e.get("handle", "")
            created = e.get("created_at", "")
            detail_parts = [
                f"Fan intent: {e['fan_intent']}" for _ in [1] if e.get("fan_intent")
            ] + [
                f"Post type: {e['post_type_classification']}" for _ in [1] if e.get("post_type_classification")
            ] + [
                f"Mirror tier: {e['mirror_tier']}" for _ in [1] if e.get("mirror_tier")
            ]

            items.append({
                "id": doc.id,
                "type": etype,
                "subtype": subtype,
                "description": _describe_event(etype, subtype, h, e.get("direction", "")),
                "handle": f"@{h.lstrip('@')}" if h else "",
                "created_at": created,
                "ago": _ago_string(created),
                "detail": " · ".join(detail_parts),
            })
    except Exception:
        pass

    return {"items": items}


def _describe_event(etype: str, subtype: str, handle: str, direction: str) -> str:
    h = f"@{handle.lstrip('@')}" if handle else "someone"
    if etype == "reply" and direction == "inbound":
        return f"{h} replied"
    if etype == "reply" and direction == "outbound":
        return f"Reply sent to {h}" + (f" ({subtype})" if subtype else "")
    if etype == "dm" and direction == "outbound":
        return f"DM sent to {h}" + (f" ({subtype})" if subtype else "")
    if etype == "dm" and direction == "inbound":
        return f"{h} replied in DMs"
    if etype == "comment":
        return f"Comment posted on {h}'s post"
    if etype == "like":
        return f"{h} liked a post"
    if etype == "repost":
        return f"{h} reposted"
    if etype == "follow":
        return f"{h} followed"
    return f"{etype} {subtype}".strip()


@app.get("/api/activity/paused", dependencies=[_AUTH])
def get_activity_paused():
    items = []
    now = _utc_now()

    try:
        for doc in db.collection("conversations").where(filter=_FF("human_handoff", "==", True)).stream():
            data = doc.to_dict() or {}
            handle = data.get("handle", doc.id)
            paused_since = data.get("handoff_at") or data.get("last_message_at") or now.isoformat()
            try:
                paused_minutes = int((now - datetime.fromisoformat(paused_since.replace("Z", "+00:00"))).total_seconds() / 60)
            except Exception:
                paused_minutes = 0
            initials = "".join(p[0].upper() for p in handle.lstrip("@").split(".")[:2]) or "??"
            items.append({
                "handle": f"@{handle.lstrip('@')}",
                "initials": initials,
                "reason": data.get("handoff_reason", "flagged for review"),
                "paused_since": paused_since,
                "paused_minutes": paused_minutes,
                "pause_type": "handoff",
            })
    except Exception:
        pass

    state = _read_state()
    seen_handles = {item["handle"].lstrip("@") for item in items}
    for h in state.get("paused_users", []):
        h = h.lstrip("@")
        if h in seen_handles:
            continue
        initials = "".join(p[0].upper() for p in h.split(".")[:2]) or "??"
        items.append({
            "handle": f"@{h}",
            "initials": initials,
            "reason": "manually paused",
            "paused_since": "",
            "paused_minutes": 0,
            "pause_type": "manual",
        })

    return {"count": len(items), "items": items}


@app.post("/api/activity/resume/{handle}", dependencies=[_AUTH])
async def post_resume(handle: str):
    handle = handle.lstrip("@")
    now_iso = _utc_now().isoformat()

    try:
        for doc in db.collection("conversations").where(filter=_FF("handle", "==", handle)).stream():
            doc.reference.update({"human_handoff": False})
    except Exception:
        pass

    state = _read_state()
    paused = [p for p in state.get("paused_users", []) if p.lstrip("@") != handle]
    _write_state({"paused_users": paused})

    return {"handle": f"@{handle}", "resumed_at": now_iso, "success": True}


# ---------------------------------------------------------------------------
# GET /api/insights
# ---------------------------------------------------------------------------

_INSIGHTS_TTL = 3600


@app.get("/api/insights", dependencies=[_AUTH])
def get_insights(range: str = "24h"):
    try:
        cache_doc = db.collection("_system").document("insights_cache").get()
        if cache_doc.exists:
            cache = cache_doc.to_dict() or {}
            cached_at = cache.get("cached_at", "")
            if cached_at:
                age = (_utc_now() - datetime.fromisoformat(cached_at.replace("Z", "+00:00"))).total_seconds()
                if age < _INSIGHTS_TTL and cache.get("html"):
                    return {"html": cache["html"], "cached": True, "cached_at": cached_at}
    except Exception:
        pass

    now = _utc_now()
    start_dt = now - timedelta(hours=24)
    try:
        events = [d.to_dict() for d in db.collection("engagement_events").where(filter=_FF("created_at", ">=", start_dt.isoformat())).stream()]
    except Exception:
        events = []

    settings_doc = db.collection("_system").document("settings").get()
    settings = settings_doc.to_dict() or {} if settings_doc.exists else {}
    discount_cap = int(settings.get("caps", {}).get("max_discounts_per_day", 5))

    buying_signals  = sum(1 for e in events if e.get("fan_intent") in ("buying_signal", "curious"))
    sub_guard       = sum(1 for e in events if e.get("reply_type") == "subscriber_warmth")
    discounts_sent  = sum(1 for e in events if e.get("interaction_subtype") == "discount_sent")
    handoffs        = sum(1 for e in events if e.get("interaction_subtype") == "handoff")
    follows_in      = sum(1 for e in events if e.get("type") == "follow" and e.get("direction") == "inbound")

    parts = []

    if buying_signals > 0:
        parts.append(f"<strong>{buying_signals} buying signal{'s' if buying_signals != 1 else ''}</strong> detected today.")
    else:
        parts.append("No buying signals yet today.")

    if sub_guard > 0:
        parts.append(f"Funnel skipped on <strong>{sub_guard} possible subscriber{'s' if sub_guard != 1 else ''}</strong>.")
    else:
        parts.append("No subscriber guards fired today.")

    if discounts_sent > 0 and discount_cap > 0:
        pct = round(discounts_sent / discount_cap * 100)
        parts.append(f"<strong>{discounts_sent} discount{'s' if discounts_sent != 1 else ''} sent</strong> — {pct}% of daily cap.")
    else:
        parts.append("No discounts sent today.")

    if handoffs > 0:
        parts.append(f"<strong>{handoffs} conversation{'s' if handoffs != 1 else ''}</strong> waiting for your personal reply.")
    else:
        parts.append("No handoffs today.")

    if follows_in > 0:
        parts.append(f"<strong>{follows_in} new follower{'s' if follows_in != 1 else ''}</strong> in the last 24h.")
    else:
        parts.append("No new followers in the last 24h.")

    html = "  ".join(parts)
    now_iso = _utc_now().isoformat()
    try:
        db.collection("_system").document("insights_cache").set({"html": html, "cached_at": now_iso})
    except Exception:
        pass

    return {"html": html, "cached": False, "cached_at": now_iso}


# ---------------------------------------------------------------------------
# GET /api/posts   GET /api/posts/{uri:path}
# ---------------------------------------------------------------------------


def _load_post_cache() -> dict:
    try:
        doc = db.document("_system/post_cache").get()
        if doc.exists:
            return doc.to_dict().get("cache", {})
    except Exception:
        pass
    return {}


def _save_post_cache(cache: dict):
    try:
        db.document("_system/post_cache").set({"cache": cache})
    except Exception:
        pass


def _fetch_post_images(uris: list) -> dict:
    """Batch-fetch image URLs from the public Bluesky API (no auth required)."""
    result = {}
    for i in range(0, len(uris), 25):
        batch = uris[i : i + 25]
        try:
            params = "&".join(f"uris[]={urllib.parse.quote(u, safe='')}" for u in batch)
            req = urllib.request.Request(
                f"https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts?{params}",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            for post in data.get("posts", []):
                uri = post.get("uri")
                image_url = None
                embed = post.get("embed") or {}
                etype = embed.get("$type", "")
                if etype == "app.bsky.embed.video#view":
                    image_url = embed.get("thumbnail")
                elif etype == "app.bsky.embed.images#view":
                    imgs = embed.get("images", [])
                    if imgs:
                        image_url = imgs[0].get("thumb")
                elif etype == "app.bsky.embed.recordWithMedia#view":
                    media = embed.get("media") or {}
                    if media.get("$type") == "app.bsky.embed.video#view":
                        image_url = media.get("thumbnail")
                    elif media.get("$type") == "app.bsky.embed.images#view":
                        imgs = media.get("images", [])
                        if imgs:
                            image_url = imgs[0].get("thumb")
                result[uri] = image_url
        except Exception:
            pass
    return result


def _attach_image_urls(posts: list) -> list:
    """Attach image_url to each post dict, using cache and fetching missing entries."""
    uris = [p["uri"] for p in posts]
    cache = _load_post_cache()
    uncached = [u for u in uris if u not in cache]
    if uncached:
        fetched = _fetch_post_images(uncached)
        cache.update(fetched)
        _save_post_cache(cache)
    for p in posts:
        p["image_url"] = cache.get(p["uri"])
    return posts


@app.get("/api/posts", dependencies=[_AUTH])
def get_posts(range: str = "7d", sort: str = "recent", type: str = "all", period: Optional[str] = None):
    if period:
        bucket = next((b for b in _range_buckets(range) if b["label"] == period), None)
        if bucket:
            try:
                events = [d.to_dict() for d in db.collection("engagement_events")
                    .where(filter=_FF("created_at", ">=", bucket["start"].isoformat()))
                    .where(filter=_FF("created_at", "<", bucket["end"].isoformat()))
                    .stream()]
            except Exception:
                events = []
        else:
            events = []
    else:
        start_dt, _ = _range_bounds(range)
        try:
            events = [d.to_dict() for d in db.collection("engagement_events").where(filter=_FF("created_at", ">=", start_dt.isoformat())).stream()]
        except Exception:
            events = []

    # Pass 1: seed from Sean's own posts only (type="post" events written by _snapshot_my_posts)
    posts_map: dict = {}
    for e in events:
        if e.get("type") != "post":
            continue
        uri = e.get("post_uri")
        if not uri or uri in posts_map:
            continue
        posts_map[uri] = {
            "uri": uri,
            "text": e.get("post_text", ""),
            "image_url": None,
            "post_type": e.get("post_type_classification", ""),
            "created_at": e.get("created_at", ""),
            "fan_replies": 0,
            "dm_pulls": 0,
        }

    # Pass 2: overlay engagement metrics for known posts only
    for e in events:
        uri = e.get("post_uri")
        if not uri or uri not in posts_map:
            continue
        if e.get("type") == "reply" and e.get("direction") == "outbound":
            posts_map[uri]["fan_replies"] += 1
        if e.get("reply_type") == "dm_pull":
            posts_map[uri]["dm_pulls"] += 1

    posts = list(posts_map.values())

    if type == "promo":
        posts = [p for p in posts if p["post_type"] == "promotional"]
    elif type == "personal":
        posts = [p for p in posts if p["post_type"] in ("personal", "casual")]

    for p in posts:
        p["dm_pull_rate_pct"] = round(p["dm_pulls"] / p["fan_replies"] * 100) if p["fan_replies"] else 0
        p["comments_posted"] = 0
        try:
            dt = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")).astimezone(_PT)
            p["created_label"] = f"{dt.strftime('%a')} {dt.hour}:{dt.minute:02d}"
        except Exception:
            p["created_label"] = ""

    if sort == "dm_pulls":
        posts.sort(key=lambda p: p["dm_pulls"], reverse=True)
    elif sort == "replies":
        posts.sort(key=lambda p: p["fan_replies"], reverse=True)
    else:
        posts.sort(key=lambda p: p.get("created_at", ""), reverse=True)

    return {"range": range, "sort": sort, "type": type, "period_filter": period, "posts": _attach_image_urls(posts[:50])}


@app.get("/api/posts/{uri:path}", dependencies=[_AUTH])
def get_post(uri: str):
    uri = urllib.parse.unquote(uri)
    try:
        docs = list(db.collection("engagement_events").where(filter=_FF("post_uri", "==", uri)).stream())
        events = [d.to_dict() for d in docs]
    except Exception:
        events = []

    if not events:
        raise HTTPException(status_code=404, detail="Post not found")

    fan_replies = sum(1 for e in events if e.get("type") == "reply" and e.get("direction") == "outbound")
    dm_pulls = sum(1 for e in events if e.get("reply_type") == "dm_pull")
    nudges = sum(1 for e in events if e.get("reply_type") == "nudge")
    intent_signals = sum(1 for e in events if e.get("direction") == "inbound" and e.get("fan_intent") in ("buying_signal", "curious"))

    first_e = min(events, key=lambda e: e.get("created_at", ""))
    created_at = first_e.get("created_at", "")
    post_type = first_e.get("post_type_classification", "")

    created_label = ""
    try:
        created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        created_label = f"{created_dt.strftime('%a')} {created_dt.hour}:{created_dt.minute:02d}"
    except Exception:
        created_dt = None

    hourly = [0] * 6
    if created_dt:
        for e in events:
            if e.get("type") == "reply" and e.get("direction") == "inbound":
                try:
                    dt = datetime.fromisoformat(e["created_at"].replace("Z", "+00:00"))
                    offset = int((dt - created_dt).total_seconds() / 3600)
                    if 0 <= offset < 6:
                        hourly[offset] += 1
                except Exception:
                    pass

    cache = _load_post_cache()
    if uri not in cache:
        fetched = _fetch_post_images([uri])
        cache.update(fetched)
        _save_post_cache(cache)

    return {
        "uri": uri,
        "text": first_e.get("post_text", ""),
        "image_url": cache.get(uri),
        "post_type": post_type,
        "created_at": created_at,
        "created_label": created_label,
        "fan_replies": fan_replies,
        "dm_pulls": dm_pulls,
        "dm_pull_rate_pct": round(dm_pulls / fan_replies * 100) if fan_replies else 0,
        "nudge_intent_rate_pct": round(intent_signals / nudges * 100) if nudges else 0,
        "comments_posted": 0,
        "engagement_peak_offset_hrs": hourly.index(max(hourly)) if any(hourly) else 0,
        "hourly_replies": hourly,
    }


# ---------------------------------------------------------------------------
# GET /api/handles?q=   (typeahead — handles with logged activity)
# ---------------------------------------------------------------------------


@app.get("/api/handles", dependencies=[_AUTH])
def list_handles(q: str = ""):
    """Return up to 20 handles from conversations that match the query prefix."""
    q = q.lstrip("@").lower()
    try:
        if q:
            docs = (
                db.collection("conversations")
                .where(filter=_FF("fan_handle", ">=", q))
                .where(filter=_FF("fan_handle", "<=", q + "\uffff"))
                .limit(20)
                .stream()
            )
        else:
            docs = db.collection("conversations").order_by("last_message_at", direction="DESCENDING").limit(20).stream()
        results = []
        for d in docs:
            data = d.to_dict() or {}
            results.append({
                "handle": data.get("fan_handle", d.id),
                "user_type": data.get("user_type", "fan"),
                "stage": data.get("stage"),
                "human_handoff": data.get("human_handoff", False),
            })
        return {"handles": results}
    except Exception:
        return {"handles": []}


# ---------------------------------------------------------------------------
# GET /api/user/{handle}
# ---------------------------------------------------------------------------


@app.get("/api/user/{handle}", dependencies=[_AUTH])
def get_user(handle: str):
    handle = handle.lstrip("@")
    convo_data: dict = {}
    messages: list = []

    try:
        doc_ref = db.collection("conversations").document(handle)
        doc = doc_ref.get()
        if doc.exists:
            convo_data = doc.to_dict() or {}
            messages = [
                d.to_dict()
                for d in doc_ref.collection("messages").order_by("timestamp").limit(50).stream()
            ]
    except Exception:
        pass

    engagement_history: list = []
    try:
        engagement_history = [
            {
                "type": (d.to_dict() or {}).get("type"),
                "post_uri": (d.to_dict() or {}).get("post_uri"),
                "created_at": (d.to_dict() or {}).get("created_at"),
            }
            for d in db.collection("engagement_events")
            .where(filter=_FF("handle", "==", handle))
            .order_by("created_at", direction="DESCENDING")
            .limit(20)
            .stream()
        ]
    except Exception:
        pass

    return {
        "handle": f"@{handle}",
        "user_type": convo_data.get("user_type", "fan"),
        "follower_count": convo_data.get("follower_count"),
        "classified_at": convo_data.get("classified_at"),
        "human_handoff": convo_data.get("human_handoff", False),
        "conversation": {
            "exchange_count": convo_data.get("exchange_count", 0),
            "stage": convo_data.get("stage"),
            "last_message_at": convo_data.get("last_message_at"),
        },
        "messages": messages,
        "engagement_history": engagement_history,
    }


# ---------------------------------------------------------------------------
# POST /api/user/{handle}/dm   (queue a manual outbound DM)
# ---------------------------------------------------------------------------


@app.post("/api/user/{handle}/dm", dependencies=[_AUTH])
async def send_user_dm(handle: str, request: Request):
    body = await request.json()
    message = (body.get("message") or "").strip()
    handle = handle.lstrip("@")
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    doc_ref = db.collection("conversations").document(handle)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="No conversation found for this handle")
    now = _utc_now().isoformat()
    doc_ref.update({
        "pending_manual_reply": message,
        "has_pending_manual_reply": True,
        "last_message_at": now,
    })
    return {"queued": True}


# ---------------------------------------------------------------------------
# Classifier endpoints
# ---------------------------------------------------------------------------

_CLASSIFIER_META = {
    "intent":   {"label": "Fan intent classifier",     "classifies": "buying_signal · curious · casual · negative",    "field": "fan_intent"},
    "posttype": {"label": "Post type classifier",       "classifies": "promotional · content · personal · casual",      "field": "post_type_classification"},
    "subguard": {"label": "Subscriber guard",           "classifies": "subscriber · non-subscriber",                   "field": "is_subscriber"},
    "handoff":  {"label": "Human handoff detector",     "classifies": "handoff · continue",                             "field": "interaction_subtype"},
}


@app.get("/api/classifier/{classifier_type}/stats", dependencies=[_AUTH])
def get_classifier_stats(classifier_type: str):
    if classifier_type not in _CLASSIFIER_META:
        raise HTTPException(status_code=404, detail=f"Unknown classifier: {classifier_type}")
    meta = _CLASSIFIER_META[classifier_type]

    accuracy_pct = 0
    trend_pct = 0
    labeled_this_month = 0
    corrections_this_month = 0

    try:
        stats_doc = db.collection("_system").document("classifier_stats").get()
        if stats_doc.exists:
            data = (stats_doc.to_dict() or {}).get(classifier_type, {})
            accuracy_pct = data.get("accuracy_pct", 0)
            trend_pct = data.get("trend_pct", 0)
            labeled_this_month = data.get("labeled_this_month", 0)
            corrections_this_month = data.get("corrections_this_month", 0)
    except Exception:
        pass

    health = "healthy" if accuracy_pct >= 85 and trend_pct >= -3 else ("critical" if accuracy_pct < 70 else "needs_attention")
    return {
        "type": classifier_type,
        "label": meta["label"],
        "accuracy_pct": accuracy_pct,
        "trend_pct": abs(trend_pct),
        "trend_direction": "up" if trend_pct >= 0 else "down",
        "labeled_this_month": labeled_this_month,
        "corrections_this_month": corrections_this_month,
        "health": health,
        "pending_review_count": 0,
        "classifies": meta["classifies"],
    }


@app.get("/api/classifier/{classifier_type}/session", dependencies=[_AUTH])
def get_classifier_session(classifier_type: str, limit: int = 10):
    if classifier_type not in _CLASSIFIER_META:
        raise HTTPException(status_code=404, detail=f"Unknown classifier: {classifier_type}")
    meta = _CLASSIFIER_META[classifier_type]
    field = meta["field"]

    items = []
    questions = {
        "intent": "Is this fan intent classification correct?",
        "posttype": "Is this post type classification correct?",
        "subguard": "Is this subscriber detection correct?",
        "handoff": "Was this correctly flagged for human handoff?",
    }

    try:
        docs = list(
            db.collection("engagement_events")
            .where(filter=_FF(field, "!=", None))
            .order_by(field)
            .order_by("created_at", direction="DESCENDING")
            .limit(limit * 3)
            .stream()
        )
        sample = random.sample(docs, min(limit, len(docs)))
        for doc in sample:
            e = doc.to_dict() or {}
            ctx = " · ".join(filter(None, [
                "Fan reply" if e.get("type") == "reply" else ("DM" if e.get("type") == "dm" else ""),
                (e.get("post_type_classification", "") + " post") if e.get("post_type_classification") else "",
                e.get("interaction_subtype", ""),
            ]))
            items.append({
                "id": doc.id,
                "context": ctx,
                "text": (e.get("fan_message") or e.get("post_text", ""))[:200],
                "current_classification": e.get(field, ""),
                "question": questions.get(classifier_type, "Is this classification correct?"),
            })
    except Exception:
        pass

    return {"type": classifier_type, "items": items}


@app.post("/api/classifier/{classifier_type}/label", dependencies=[_AUTH])
async def post_classifier_label(classifier_type: str, request: Request):
    if classifier_type not in _CLASSIFIER_META:
        raise HTTPException(status_code=404, detail=f"Unknown classifier: {classifier_type}")
    body = await request.json()
    item_id = body.get("item_id")
    label = body.get("label")  # true | false | null
    now_iso = _utc_now().isoformat()

    try:
        db.collection("_system").document("classifier_labels").collection("records").add({
            "classifier_type": classifier_type,
            "item_id": item_id,
            "label": label,
            "at": now_iso,
        })

        if label in (True, False):
            stats_ref = db.collection("_system").document("classifier_stats")
            stats_doc = stats_ref.get()
            stats = stats_doc.to_dict() or {} if stats_doc.exists else {}
            ts = stats.get(classifier_type, {})
            ts["labeled_this_month"] = ts.get("labeled_this_month", 0) + 1
            if label is False:
                ts["corrections_this_month"] = ts.get("corrections_this_month", 0) + 1
            total = ts.get("labeled_this_month", 1)
            corrections = ts.get("corrections_this_month", 0)
            ts["accuracy_pct"] = round((total - corrections) / total * 100) if total else 0
            stats[classifier_type] = ts
            stats_ref.set(stats)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"item_id": item_id, "recorded": True, "session_complete": False}


# ---------------------------------------------------------------------------
# Brand voice endpoints
# ---------------------------------------------------------------------------


@app.get("/api/brand-voice", dependencies=[_AUTH])
def get_brand_voice():
    try:
        doc = db.collection("_system").document("brand_voice").get()
        if doc.exists:
            return doc.to_dict() or {}
    except Exception:
        pass
    path = os.getenv("BRANDVOICE_PATH")
    if path:
        try:
            with open(path) as f:
                return {"raw_md": f.read(), "version": 0, "pushed_at": None}
        except Exception:
            pass
    raise HTTPException(status_code=404, detail="Brand voice not configured")


@app.post("/api/brand-voice", dependencies=[_AUTH])
async def post_brand_voice(request: Request):
    body = await request.json()
    identity = body.get("identity", {})
    if not identity.get("creator_name") or not identity.get("handle") or not identity.get("persona_summary"):
        raise HTTPException(status_code=400, detail="Required: identity.creator_name, handle, persona_summary")

    now_iso = _utc_now().isoformat()
    current_version = 0
    try:
        doc = db.collection("_system").document("brand_voice").get()
        if doc.exists:
            current_version = (doc.to_dict() or {}).get("version", 0)
    except Exception:
        pass

    new_version = current_version + 1
    body["version"] = new_version
    body["pushed_at"] = now_iso

    try:
        db.collection("_system").document("brand_voice").set(body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Firestore write failed: {e}")

    sections_changed = [k for k in body if k not in ("version", "pushed_at", "template_version", "schema_version")]
    try:
        db.collection("_system").document("brand_voice_history").collection("pushes").add({
            "version": new_version,
            "pushed_at": now_iso,
            "sections_changed": sections_changed,
            "pushed_by": "dashboard",
        })
    except Exception:
        pass

    # Push rendered markdown to Secret Manager (optional — silently no-ops if not configured)
    try:
        from google.cloud import secretmanager
        sm = secretmanager.SecretManagerServiceClient()
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "sx-platform")
        md = render_brand_voice_md(body)
        sm.add_secret_version(
            request={
                "parent": f"projects/{project}/secrets/brandvoice-content",
                "payload": {"data": md.encode("utf-8")},
            }
        )
    except Exception:
        pass

    return {"version": new_version, "pushed_at": now_iso}


@app.get("/api/brand-voice/history", dependencies=[_AUTH])
def get_brand_voice_history():
    history: list = []
    try:
        docs = list(
            db.collection("_system")
            .document("brand_voice_history")
            .collection("pushes")
            .order_by("pushed_at", direction="DESCENDING")
            .limit(10)
            .stream()
        )
        history = [d.to_dict() for d in docs]
    except Exception:
        pass
    return {"history": history}


_BV_PREVIEW_CACHE: dict = {}
_BV_PREVIEW_TTL = 600


@app.post("/api/brand-voice/preview", dependencies=[_AUTH])
async def post_brand_voice_preview(request: Request):
    body = await request.json()
    cache_key = hashlib.md5(json.dumps(body, sort_keys=True).encode()).hexdigest()
    cached = _BV_PREVIEW_CACHE.get(cache_key)
    if cached and (time.time() - cached["at"]) < _BV_PREVIEW_TTL:
        return {"previews": cached["previews"]}

    brand_voice_md = render_brand_voice_md(body)
    scenarios = {
        "nudge": "A fan just replied: 'This is exactly my type, love your vibe'. Write a nudge reply (1 question to steer toward DMs). ONLY the reply text.",
        "dm": "A fan just reposted your post. Write a warm thank-you DM. ONLY the message text.",
        "comment": "You're commenting on a fitness creator's post: 'New PR today — 315 deadlift. Consistency is everything.' Write a short comment. ONLY the comment text.",
        "peer": "A fellow creator (@alexfit, 25k followers) replied: 'Love what you put out — DMs open if you ever want to collab'. Write a peer reply. ONLY the reply text.",
    }

    previews: dict = {}
    try:
        client = _anthropic.Anthropic()
        for scenario_type, scenario_prompt in scenarios.items():
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                system=brand_voice_md,
                messages=[{"role": "user", "content": scenario_prompt}],
            )
            write_cost_event(db, msg.model, msg.usage, "brand_voice_preview")
            previews[scenario_type] = msg.content[0].text.strip()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    _BV_PREVIEW_CACHE[cache_key] = {"previews": previews, "at": time.time()}
    return {"previews": previews}


# ---------------------------------------------------------------------------
# POST /api/query
# ---------------------------------------------------------------------------

_QUERY_CACHE: dict = {}
_QUERY_TTL = 300

_QUERY_SCHEMA = """\
You are a data query assistant for the SX Platform Bluesky bot. Output JSON only — no prose, no markdown fences.

Collections:
engagement_events: {type, handle, post_uri, direction, created_at, reply_type, fan_intent, post_type_classification, interaction_subtype, mirror_tier, model, token_usage_input, token_usage_output}
conversations: {handle, user_type, exchange_count, human_handoff, stage, last_message_at}
dm_queue: {handle, status, dm_type, created_at, sent_at}
comment_queue: {target_handle, post_uri, status, queued_at, posted_at}
api_cost_events: {provider, model, call_type, input_tokens, output_tokens, cost_usd, created_at}
target_accounts: {handle, tier, domain, quality_flag, created_at}
function_runs: {function, run_at, date, status, error_msg, duration_s, metrics}

Today: {TODAY}. Range: {RANGE}. Page: {PAGE}.

JSON schema to output:
{"collection":"...","filters":[{"field":"...","op":"==|>=|<=|in","value":"..."}],"order_by":"...","order_dir":"asc|desc","limit":50,"answer_fields":["..."],"needs_table":true,"summary_instruction":"..."}\
"""


@app.post("/api/query", dependencies=[_AUTH])
async def post_query(request: Request):
    body = await request.json()
    question = (body.get("question") or "").strip()
    context_range = body.get("context_range", "24h")
    context_page = body.get("context_page", "dashboard")

    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    now_minute = _utc_now().strftime("%Y-%m-%dT%H:%M")
    cache_key = hashlib.md5(f"{question}|{context_range}|{context_page}|{now_minute}".encode()).hexdigest()
    if cache_key in _QUERY_CACHE:
        return _QUERY_CACHE[cache_key]

    t0 = time.time()
    today = _utc_now().date().isoformat()
    try:
        client = _anthropic.Anthropic()
    except Exception as e:
        return {"error": f"Anthropic client error: {e}", "question": question}

    # Special case: brand voice page
    bv_keywords = {"banned", "approved", "vocab", "word", "rule", "archetype", "tone", "lexicon", "persona", "voice"}
    if context_page == "brand_voice" and any(kw in question.lower() for kw in bv_keywords):
        try:
            bv_doc = db.collection("_system").document("brand_voice").get()
            bv = bv_doc.to_dict() or {} if bv_doc.exists else {}
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": f"Brand voice:\n{json.dumps(bv, indent=2)}\n\nAnswer in 1-3 sentences: {question}"}],
            )
            write_cost_event(db, msg.model, msg.usage, "query_bar")
            result = {"question": question, "prose": msg.content[0].text.strip(), "table": None, "has_table": False, "query_took_ms": int((time.time() - t0) * 1000)}
            _QUERY_CACHE[cache_key] = result
            return result
        except Exception as e:
            return {"error": f"query failed: {e}", "question": question}

    # Special case: settings page
    if context_page == "settings":
        try:
            settings = _load_settings()
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": f"Settings:\n{json.dumps(settings, indent=2)}\n\nAnswer in 1-2 sentences: {question}"}],
            )
            write_cost_event(db, msg.model, msg.usage, "query_bar")
            result = {"question": question, "prose": msg.content[0].text.strip(), "table": None, "has_table": False, "query_took_ms": int((time.time() - t0) * 1000)}
            _QUERY_CACHE[cache_key] = result
            return result
        except Exception as e:
            return {"error": f"query failed: {e}", "question": question}

    # Step 1: translate question to query plan
    schema_prompt = _QUERY_SCHEMA.format(TODAY=today, RANGE=context_range, PAGE=context_page)
    try:
        step1 = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=schema_prompt,
            messages=[{"role": "user", "content": question}],
        )
        write_cost_event(db, step1.model, step1.usage, "query_bar")
        plan_text = step1.content[0].text.strip().strip("`").removeprefix("json").strip()
        plan = json.loads(plan_text)
    except Exception:
        return {"error": "couldn't interpret that — try rephrasing", "question": question}

    # Step 2: execute Firestore query
    try:
        rows = _execute_query_plan(plan)
    except Exception as e:
        return {"error": f"query failed: {e}", "question": question}

    # Step 3: summarise
    prose = f"Found {len(rows)} result(s)."
    try:
        summary_instruction = plan.get("summary_instruction", "Summarise these results in 1-3 sentences.")
        step2 = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": f"Data: {json.dumps(rows[:20])}\n\n{summary_instruction}"}],
        )
        write_cost_event(db, step2.model, step2.usage, "query_bar")
        prose = step2.content[0].text.strip()
    except Exception:
        pass

    table = None
    has_table = plan.get("needs_table", False)
    if has_table and rows:
        answer_fields = plan.get("answer_fields", list(rows[0].keys())[:3])
        table = {
            "heads": answer_fields,
            "rows": [[str(r.get(f, "")) for f in answer_fields] for r in rows[:20]],
        }

    result = {"question": question, "prose": prose, "table": table, "has_table": bool(table), "query_took_ms": int((time.time() - t0) * 1000)}
    _QUERY_CACHE[cache_key] = result
    return result


def _execute_query_plan(plan: dict) -> list:
    collection = plan.get("collection", "engagement_events")
    filters = plan.get("filters", [])
    order_by = plan.get("order_by")
    order_dir = plan.get("order_dir", "desc")
    limit = min(plan.get("limit", 50), 200)

    # Handle nested paths like "_system/follower_snapshots/daily"
    parts = collection.split("/")
    if len(parts) == 3:
        query = db.collection(parts[0]).document(parts[1]).collection(parts[2])
    else:
        query = db.collection(parts[0])

    op_map = {"==": "==", ">=": ">=", "<=": "<=", ">": ">", "<": "<", "in": "in", "array-contains": "array_contains"}
    for f in filters:
        field, op, value = f.get("field"), op_map.get(f.get("op", "=="), "=="), f.get("value")
        if field and value is not None:
            query = query.where(filter=_FF(field, op, value))

    if order_by:
        query = query.order_by(order_by, direction="DESCENDING" if order_dir == "desc" else "ASCENDING")

    return [{"id": d.id, **d.to_dict()} for d in query.limit(limit).stream()]


# ---------------------------------------------------------------------------
# GET /api/spend/summary   GET /api/spend
# ---------------------------------------------------------------------------

_GCP_CF = {"invocation": 0.0000004, "gb_second": 0.0000025, "avg_duration_s": 8, "memory_gb": 0.25}
_GCP_FS = {"read": 0.00000006, "write": 0.00000018}


def _gcp_estimate(period_start: datetime) -> dict:
    try:
        invocations = sum(
            1 for _ in db.collection("function_runs")
            .where(filter=_FF("run_at", ">=", period_start.isoformat()))
            .stream()
        )
        cf = invocations * (_GCP_CF["invocation"] + _GCP_CF["avg_duration_s"] * _GCP_CF["memory_gb"] * _GCP_CF["gb_second"])
        fs = invocations * (20 * _GCP_FS["read"] + 2 * _GCP_FS["write"])
        return {"gcp_functions": round(cf, 4), "gcp_firestore": round(fs, 4), "gcp_other": 0.0}
    except Exception:
        return {"gcp_functions": 0.0, "gcp_firestore": 0.0, "gcp_other": 0.0}


@app.get("/api/spend/summary", dependencies=[_AUTH])
def get_spend_summary():
    now = _utc_now()
    this_week_start = now - timedelta(days=7)
    last_week_start = now - timedelta(days=14)
    this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_cap = _load_settings().get("caps", {}).get("monthly_spend_cap_usd")

    try:
        all_events = [
            d.to_dict()
            for d in db.collection("api_cost_events")
            .where(filter=_FF("created_at", ">=", last_week_start.isoformat()))
            .stream()
        ]
    except Exception:
        all_events = []

    this_week = sum(e.get("cost_usd", 0) for e in all_events if (e.get("created_at") or "") >= this_week_start.isoformat())
    last_week = sum(e.get("cost_usd", 0) for e in all_events if (e.get("created_at") or "") < this_week_start.isoformat())
    this_month = sum(e.get("cost_usd", 0) for e in all_events if (e.get("created_at") or "") >= this_month_start.isoformat())

    trend_pct = round((this_week - last_week) / last_week * 100) if last_week else 0
    gcp = _gcp_estimate(this_week_start)

    by_type: dict = {}
    for e in all_events:
        ct = e.get("call_type", "other")
        by_type[ct] = by_type.get(ct, 0) + e.get("cost_usd", 0)
    top_driver = max(by_type, key=by_type.get) if by_type else ""

    return {
        "this_week_usd": round(this_week, 4),
        "last_week_usd": round(last_week, 4),
        "trend_pct": abs(trend_pct),
        "trend_direction": "up" if trend_pct >= 0 else "down",
        "this_month_usd": round(this_month, 4),
        "monthly_cap_usd": monthly_cap,
        "breakdown": {"anthropic": round(this_week, 4), **{k: round(v, 4) for k, v in gcp.items()}},
        "top_cost_driver": top_driver,
        "top_cost_driver_usd": round(by_type.get(top_driver, 0), 4),
    }


@app.get("/api/spend", dependencies=[_AUTH])
def get_spend(range: str = "7d"):
    start_dt, _ = _range_bounds(range)
    try:
        cost_events = [
            d.to_dict()
            for d in db.collection("api_cost_events")
            .where(filter=_FF("created_at", ">=", start_dt.isoformat()))
            .stream()
        ]
    except Exception:
        cost_events = []

    bucket_data = []
    for b in _range_buckets(range):
        in_b = _in_bucket(cost_events, b)
        anthropic_usd = sum(e.get("cost_usd", 0) for e in in_b)
        gcp = _gcp_estimate(b["start"])
        gcp_usd = gcp["gcp_functions"] + gcp["gcp_firestore"]
        call_breakdown: dict = {}
        for e in in_b:
            ct = e.get("call_type", "other")
            call_breakdown[ct] = call_breakdown.get(ct, 0) + e.get("cost_usd", 0)
        bucket_data.append({
            "label": b["label"],
            "date": b["start"].date().isoformat(),
            "anthropic_usd": round(anthropic_usd, 4),
            "gcp_usd": round(gcp_usd, 4),
            "total_usd": round(anthropic_usd + gcp_usd, 4),
            "call_breakdown": {k: round(v, 4) for k, v in call_breakdown.items()},
        })

    by_model: dict = {}
    by_call_type: dict = {}
    for e in cost_events:
        m = e.get("model", "unknown")
        ct = e.get("call_type", "other")
        if m not in by_model:
            by_model[m] = {"calls": 0, "cost_usd": 0.0}
        by_model[m]["calls"] += 1
        by_model[m]["cost_usd"] += e.get("cost_usd", 0)
        if ct not in by_call_type:
            by_call_type[ct] = {"calls": 0, "cost_usd": 0.0}
        by_call_type[ct]["calls"] += 1
        by_call_type[ct]["cost_usd"] += e.get("cost_usd", 0)

    raw_events = [
        {
            "time": (
                datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")).astimezone(_PT).strftime("%I:%M%p").lower().lstrip("0")
                if e.get("created_at") else ""
            ),
            "call_type": e.get("call_type", ""),
            "model": e.get("model", ""),
            "input_tokens": e.get("input_tokens", 0),
            "output_tokens": e.get("output_tokens", 0),
            "cost_usd": e.get("cost_usd", 0),
        }
        for e in sorted(cost_events, key=lambda e: e.get("created_at", ""), reverse=True)[:20]
    ]

    return {
        "range": range,
        "total_usd": round(sum(b["total_usd"] for b in bucket_data), 4),
        "buckets": bucket_data,
        "by_model": {k: {"calls": v["calls"], "cost_usd": round(v["cost_usd"], 4)} for k, v in by_model.items()},
        "by_call_type": {k: {"calls": v["calls"], "cost_usd": round(v["cost_usd"], 4)} for k, v in by_call_type.items()},
        "raw_events": raw_events,
    }
