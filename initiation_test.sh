#!/usr/bin/env bash
# initiation_test.sh — Invoke all Cloud Functions once in dependency order to verify deployment.
#
# Usage:
#   ./initiation_test.sh              # Full run including discovery pipeline (~3-4 hrs)
#   ./initiation_test.sh --quick      # Skip long-running discovery pipeline (~30-90 min)
#
# Prerequisites:
#   gcloud auth login
#   gcloud config set project sx-platform

set -o pipefail   # No -e or -u; we capture failures manually

PROJECT="sx-platform"
REGION="us-central1"

# --- Flags ---
QUICK=false
for arg in "$@"; do
  [[ "$arg" == "--quick" ]] && QUICK=true
done

# --- Colors (ANSI, degrades gracefully if not a terminal) ---
if [[ -t 1 ]]; then
  BOLD='\033[1m'; GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; RESET='\033[0m'
else
  BOLD=''; GREEN=''; RED=''; YELLOW=''; RESET=''
fi

# --- Result tracking ---
PASS=0
FAIL=0
SKIP=0
declare -a RESULTS=()

# --- Helper: call a single function and record result ---
# Usage: call_fn <fn-name> <display-label> [--data='{"key":"val"}']
call_fn() {
  local name="$1"
  local label="$2"
  shift 2
  local extra_args=("$@")

  printf "  %-40s" "$label ..."
  local start=$SECONDS
  local output
  if output=$(gcloud functions call "$name" \
      --gen2 \
      --region="$REGION" \
      --project="$PROJECT" \
      "${extra_args[@]}" 2>&1); then
    local elapsed=$(( SECONDS - start ))
    printf "${GREEN}OK${RESET} (${elapsed}s)\n"
    RESULTS+=("${GREEN}PASS${RESET}  $label")
    (( PASS++ )) || true
  else
    local elapsed=$(( SECONDS - start ))
    printf "${RED}FAIL${RESET} (${elapsed}s)\n"
    # Print first 5 lines of error output indented
    echo "$output" | head -5 | sed 's/^/    /'
    RESULTS+=("${RED}FAIL${RESET}  $label")
    (( FAIL++ )) || true
  fi
}

skip_fn() {
  local label="$1"
  printf "  %-40s${YELLOW}SKIP${RESET}\n" "$label"
  RESULTS+=("${YELLOW}SKIP${RESET}  $label")
  (( SKIP++ )) || true
}

# ============================================================
# PHASE 1 — Independent quick functions (no prior data needed)
# All have ≤ 2 min configured timeout; run synchronously.
# ============================================================
echo ""
printf "${BOLD}=== Phase 1: Independent quick functions ===${RESET}\n"

call_fn "poll-notifications"   "poll-notifications"
call_fn "poll-inbound-dms"     "poll-inbound-dms"
call_fn "cleanup-stale-docs"   "cleanup-stale-docs"
call_fn "process-dm-queue"     "process-dm-queue"
# follower-graph-score before discovery just verifies the function starts cleanly
# (no target_accounts yet — it will log "nothing to score" and exit 0)
call_fn "follower-graph-score" "follower-graph-score (pre-seed smoke)"

# ============================================================
# PHASE 2 — Seed target_accounts via starter-pack-discovery.
# MUST complete before scan-comment-targets or comment pipeline.
# Configured timeout: 1800s (~30 min).
# ============================================================
echo ""
printf "${BOLD}=== Phase 2: Seed target_accounts (starter-pack-discovery) ===${RESET}\n"
echo "  Note: configured timeout is 30 min — gcloud call blocks until complete."

call_fn "starter-pack-discovery" "starter-pack-discovery"

# ============================================================
# PHASE 3 — Comment pipeline.
# scan-comment-targets reads target_accounts (seeded in Phase 2).
# execute-comment dequeues from comment_queue (populated by scan).
# ============================================================
echo ""
printf "${BOLD}=== Phase 3: Comment pipeline ===${RESET}\n"

call_fn "scan-comment-targets" "scan-comment-targets"
# execute-comment has up to 10-min jitter; if comment_queue is empty it exits fast.
call_fn "execute-comment"      "execute-comment"

# ============================================================
# PHASE 4 — DM batch.
# execute-dm-batch dequeues pending DMs.  On first/clean run the
# queue may be empty and it will return quickly.  With pending items
# it can run up to 60 min (7 × ~8 min stagger).
# ============================================================
echo ""
printf "${BOLD}=== Phase 4: DM batch ===${RESET}\n"
echo "  Note: batch_size=0 for a quick connectivity check — no DMs are sent."

call_fn "execute-dm-batch" "execute-dm-batch" --data='{"batch_size":0}'

# ============================================================
# PHASE 5 — Follower graph discovery pipeline.
# Dependency chain (must run in order):
#   follower-graph-prefetch  →  slots 0-4 (sequential)  →  follower-graph-score
# Total wall-clock: ~30 min (prefetch) + 5 × ~45 min (slots) + ~2 min (score)
# Skip with --quick flag.
# ============================================================
echo ""
printf "${BOLD}=== Phase 5: Follower graph discovery pipeline ===${RESET}\n"

if [[ "$QUICK" == "true" ]]; then
  echo "  Skipped — re-run without --quick to include the full discovery pipeline."
  skip_fn "follower-graph-prefetch"
  for slot in 0 1 2 3 4; do
    skip_fn "follower-graph-slot $slot"
  done
  skip_fn "follower-graph-score (post-discovery)"
else
  echo "  Note: total estimated time is 3-4 hours."
  echo ""

  # Prefetch: fetch + filter all fan profiles → writes ordered DID list to Firestore.
  # follower-graph-slot will fail if this hasn't run.
  call_fn "follower-graph-prefetch" "follower-graph-prefetch"

  # Slots must run sequentially — each slot reads the position written by the previous.
  for slot in 0 1 2 3 4; do
    call_fn "follower-graph-slot" "follower-graph-slot $slot" --data="{\"slot\": $slot}"
  done

  # Score: pure Firestore pass — re-tiers all target_accounts from combined slot data.
  call_fn "follower-graph-score" "follower-graph-score (post-discovery)"
fi

# ============================================================
# SUMMARY
# ============================================================
echo ""
printf "${BOLD}=== Summary ===${RESET}\n"
echo ""
for r in "${RESULTS[@]}"; do
  printf "  $r\n"
done
echo ""
printf "  ${GREEN}Passed: $PASS${RESET}  ${RED}Failed: $FAIL${RESET}  ${YELLOW}Skipped: $SKIP${RESET}\n"
echo ""
if (( FAIL > 0 )); then
  echo "  Check logs: gcloud functions logs read FUNCTION_NAME --gen2 --region=$REGION --project=$PROJECT"
  exit 1
fi
