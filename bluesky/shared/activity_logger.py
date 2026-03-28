"""
Activity logger — records every scheduled function run to Firestore.

log_run(function_name, metrics, status, error_msg, duration_s)
    Write one run record to the `function_runs` collection.
    Called at the end of each Cloud Function handler.

get_runs(function_name, period, since, until, limit)
    Query run records by function name and/or date period.
    period shortcuts: "today" | "7d" | "30d" | "month" | "all"
    since/until: ISO date strings "YYYY-MM-DD" — override period when provided.
    Returns list of dicts sorted by run_at descending.

print_summary(function_name, period)
    Console-friendly summary for a function over a period.

Firestore collection: `function_runs`
Document fields:
    function    str      Cloud Function name (e.g. "execute-comment")
    run_at      str      ISO UTC timestamp  (for range queries)
    date        str      YYYY-MM-DD         (for today / month queries)
    status      str      "ok" | "error"
    error_msg   str|None error description, None when status=="ok"
    duration_s  float    wall-clock seconds
    metrics     dict     function-specific output counts

Compound index required in Firestore:
    Collection: function_runs
    Fields: function ASC, run_at ASC
"""
from datetime import datetime, timezone, timedelta, date as _date_type


from bluesky.shared.firestore_client import db

_RUNS = db.collection("function_runs")


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def log_run(
    function_name: str,
    metrics: dict,
    status: str = "ok",
    error_msg: str = None,
    duration_s: float = None,
) -> None:
    """
    Write one run record to Firestore `function_runs`.

    Args:
        function_name: Cloud Function name (e.g. "execute-comment")
        metrics:       Function-specific output dict (counts, flags, etc.)
        status:        "ok" or "error"
        error_msg:     Exception message when status=="error", else None
        duration_s:    Wall-clock seconds the function took
    """
    now = datetime.now(timezone.utc)
    _RUNS.add({
        "function": function_name,
        "run_at": now.isoformat(),
        "date": now.date().isoformat(),
        "status": status,
        "error_msg": error_msg,
        "duration_s": round(duration_s, 1) if duration_s is not None else None,
        "metrics": metrics or {},
    })


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def _period_cutoff(period: str):
    """Return (since_iso, until_iso | None) for a named period."""
    now = datetime.now(timezone.utc)
    today = now.date()

    if period == "today":
        start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        return start.isoformat(), None

    if period == "7d":
        start = now - timedelta(days=7)
        return start.isoformat(), None

    if period == "30d":
        start = now - timedelta(days=30)
        return start.isoformat(), None

    if period == "month":
        start = datetime(today.year, today.month, 1, tzinfo=timezone.utc)
        return start.isoformat(), None

    if period == "all":
        return None, None

    raise ValueError(f"Unknown period '{period}'. Use: today | 7d | 30d | month | all")


def get_runs(
    function_name: str = None,
    period: str = "today",
    since: str = None,
    until: str = None,
    limit: int = 500,
) -> list:
    """
    Query function run records.

    Args:
        function_name: Filter to one function. None = all functions.
        period:        "today" | "7d" | "30d" | "month" | "all"
                       Ignored when since is provided.
        since:         ISO date "YYYY-MM-DD" — override period start.
        until:         ISO date "YYYY-MM-DD" — exclusive upper bound.
        limit:         Max documents to return (default 500).

    Returns:
        List of dicts sorted by run_at descending.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter as _f

    query = _RUNS

    if function_name:
        query = query.where(filter=_f("function", "==", function_name))

    # Resolve time bounds
    if since:
        since_iso = f"{since}T00:00:00+00:00"
    else:
        since_iso, _ = _period_cutoff(period)

    if since_iso:
        query = query.where(filter=_f("run_at", ">=", since_iso))

    if until:
        until_iso = f"{until}T23:59:59+00:00"
        query = query.where(filter=_f("run_at", "<=", until_iso))

    docs = list(query.order_by("run_at", direction="DESCENDING").limit(limit).stream())
    return [{"id": d.id, **d.to_dict()} for d in docs]


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------

def print_summary(function_name: str = None, period: str = "today") -> None:
    """
    Print a human-readable summary of runs for a function (or all functions)
    over the given period.
    """
    runs = get_runs(function_name=function_name, period=period)

    label = function_name or "all functions"
    print(f"\n=== {label}  [{period}]  ({len(runs)} run(s)) ===")

    if not runs:
        print("  (no runs found)")
        return

    # Group by function if querying all
    from collections import defaultdict
    by_fn: dict = defaultdict(list)
    for r in runs:
        by_fn[r["function"]].append(r)

    for fn, fn_runs in sorted(by_fn.items()):
        ok = sum(1 for r in fn_runs if r["status"] == "ok")
        err = sum(1 for r in fn_runs if r["status"] == "error")
        durations = [r["duration_s"] for r in fn_runs if r["duration_s"] is not None]
        avg_dur = sum(durations) / len(durations) if durations else None

        print(f"\n  {fn}")
        print(f"    runs: {len(fn_runs)}  ok: {ok}  error: {err}", end="")
        if avg_dur is not None:
            print(f"  avg duration: {avg_dur:.0f}s", end="")
        print()

        # Aggregate numeric metrics across all ok runs
        agg: dict = {}
        for r in fn_runs:
            if r["status"] != "ok":
                continue
            for k, v in (r.get("metrics") or {}).items():
                if isinstance(v, (int, float)):
                    agg[k] = agg.get(k, 0) + v

        if agg:
            print("    metrics (totals):")
            for k, v in sorted(agg.items()):
                print(f"      {k}: {v}")

        # Show errors if any
        errors = [r for r in fn_runs if r["status"] == "error"]
        if errors:
            print(f"    errors:")
            for r in errors[:3]:
                print(f"      {r['run_at'][:19]}Z — {r.get('error_msg', '?')}")
