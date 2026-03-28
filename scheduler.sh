#!/usr/bin/env bash
# Create (or update) Cloud Scheduler jobs for all Cloud Functions.
#
# Run once after deploy.sh. Re-running is safe — jobs are updated if they exist.
#
# Prerequisites:
#   gcloud config set project sx-platform
#   Cloud Scheduler API enabled: gcloud services enable cloudscheduler.googleapis.com

set -euo pipefail

PROJECT="sx-platform"
REGION="us-central1"
SA="$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

fn_url() {
  # Resolve the HTTPS trigger URL for a deployed gen2 function
  gcloud functions describe "$1" \
    --gen2 \
    --region="$REGION" \
    --project="$PROJECT" \
    --format="value(serviceConfig.uri)"
}

create_or_update_job() {
  local JOB_NAME="$1"
  local SCHEDULE="$2"   # cron expression
  local FN_NAME="$3"    # Cloud Function name
  local BODY="${4:-}"   # optional JSON body (e.g. '{"slot":0}')

  local URL
  URL=$(fn_url "$FN_NAME")

  # Build optional body flags — both create and update use the same flags
  local BODY_FLAGS=()
  if [[ -n "$BODY" ]]; then
    BODY_FLAGS=(--message-body "$BODY" --headers "Content-Type=application/json")
  fi

  echo "==> $JOB_NAME  ($SCHEDULE)  →  $URL"

  if gcloud scheduler jobs describe "$JOB_NAME" --location="$REGION" --project="$PROJECT" &>/dev/null; then
    gcloud scheduler jobs update http "$JOB_NAME" \
      --location="$REGION" \
      --schedule="$SCHEDULE" \
      --uri="$URL" \
      --http-method=POST \
      --oidc-service-account-email="$SA" \
      --project="$PROJECT" \
      "${BODY_FLAGS[@]+"${BODY_FLAGS[@]}"}"
  else
    gcloud scheduler jobs create http "$JOB_NAME" \
      --location="$REGION" \
      --schedule="$SCHEDULE" \
      --uri="$URL" \
      --http-method=POST \
      --oidc-service-account-email="$SA" \
      --project="$PROJECT" \
      "${BODY_FLAGS[@]+"${BODY_FLAGS[@]}"}"
  fi
}

# All times in America/Los_Angeles; adjust TZ if needed.

create_or_update_job "poll-notifications"      "*/5 * * * *"     "poll-notifications"
create_or_update_job "poll-inbound-dms"        "*/5 * * * *"     "poll-inbound-dms"
create_or_update_job "scan-comment-targets"    "*/15 * * * *"    "scan-comment-targets"
create_or_update_job "execute-comment"         "*/20 * * * *"    "execute-comment"
create_or_update_job "process-dm-queue"        "0 */2 * * *"     "process-dm-queue"
create_or_update_job "execute-dm-batch"        "0 */4 * * *"     "execute-dm-batch"
create_or_update_job "follower-graph-prefetch"  "0 1 * * 6"       "follower-graph-prefetch"          # weekly Saturday 1am
create_or_update_job "follower-graph-slot-0"    "0 2 * * *"       "follower-graph-slot"  '{"slot":0}'  # nightly 2:00am
create_or_update_job "follower-graph-slot-1"    "40 2 * * *"      "follower-graph-slot"  '{"slot":1}'  # nightly 2:40am
create_or_update_job "follower-graph-slot-2"    "20 3 * * *"      "follower-graph-slot"  '{"slot":2}'  # nightly 3:20am
create_or_update_job "follower-graph-slot-3"    "0 4 * * *"       "follower-graph-slot"  '{"slot":3}'  # nightly 4:00am
create_or_update_job "follower-graph-slot-4"    "40 4 * * *"      "follower-graph-slot"  '{"slot":4}'  # nightly 4:40am
create_or_update_job "follower-graph-score"     "30 5 * * *"      "follower-graph-score"                # nightly 5:30am (after slots)
create_or_update_job "starter-pack-discovery"  "0 3 * * 0"       "starter-pack-discovery"
create_or_update_job "cleanup-stale-docs"      "0 4 * * 0"       "cleanup-stale-docs"

echo ""
echo "All scheduler jobs created/updated."
echo "View in GCP Console: https://console.cloud.google.com/cloudscheduler?project=$PROJECT"
