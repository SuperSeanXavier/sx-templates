#!/usr/bin/env bash
# Deploy all Cloud Functions to GCP.
#
# Prerequisites:
#   gcloud auth login
#   gcloud config set project sx-platform
#
# Secrets must be created in Secret Manager before deploying:
#   gcloud secrets create bluesky-app-password --replication-policy automatic
#   gcloud secrets create anthropic-api-key    --replication-policy automatic
#   gcloud secrets create brandvoice-content   --replication-policy automatic
#
#   echo -n "<password>" | gcloud secrets versions add bluesky-app-password --data-file=-
#   echo -n "<api-key>"  | gcloud secrets versions add anthropic-api-key    --data-file=-
#   cat /path/to/SX_Instructions.md | gcloud secrets versions add brandvoice-content --data-file=-

set -euo pipefail

PROJECT="sx-platform"
REGION="us-central1"
RUNTIME="python311"
SOURCE="."   # deploy from project root so bluesky/ package is included
ENTRY_FILE="functions/main"   # module path for --entry-point resolution

# Common env vars written to a temp YAML file.
# --env-vars-file avoids gcloud's comma-delimiter conflict for values that contain commas
# (e.g. DISCOVERY_DOMAIN_KEYWORDS, DISCOVERY_DOMAINS).
ENV_FILE=$(mktemp /tmp/gcloud-env-XXXXXX.yaml)
trap "rm -f $ENV_FILE" EXIT

cat > "$ENV_FILE" <<'YAML'
BLUESKY_HANDLE: seanxavier.bsky.social
GOOGLE_CLOUD_PROJECT: sx-platform
FIRESTORE_DATABASE: sxplatformdatabase
CREATOR_DETECTION_MUTUAL_FOLLOW: "true"
CREATOR_DETECTION_BIO: "false"
CREATOR_DETECTION_FOLLOWER_COUNT: "false"
COLLAB_DM_THRESHOLD: "20000"
DAILY_DM_CAP: "50"
DAILY_COMMENT_CAP: "0"   # Commenting paused. Before re-enabling (set to 50): review and update brand voice in Firestore (_system/brand_voice) to ensure comment tone is approved for public-facing use on other creators' posts.
CLASSIFICATION_TTL_DAYS: "30"
DISCOVERY_CREATOR_HANDLE: seanxavier.bsky.social
DISCOVERY_DOMAIN_KEYWORDS: "gay fitness,muscle,gay bodybuilder,gay OnlyFans"
DISCOVERY_DOMAINS: "fitness,muscle"
FAN_DISCOUNT_CODE: Fans50
FAN_DISCOUNT_URL_LIKE: https://seanxavier.com/memberships
FAN_DISCOUNT_URL_REPOST: https://seanxavier.com/memberships
MAX_DISCOUNTS_PER_DAY: "250"
# /tmp is the only writable path in Cloud Run-backed Gen2 functions (ephemeral per container).
STATE_PATH: /tmp/state.json
DM_STATE_PATH: /tmp/dm_state.json
YAML

# Secrets mounted as env vars
COMMON_SECRETS="BLUESKY_APP_PASSWORD=bluesky-app-password:latest,ANTHROPIC_API_KEY=anthropic-api-key:latest,BRANDVOICE_CONTENT=brandvoice-content:latest"

deploy_fn() {
  local NAME="$1"        # Cloud Function name  (e.g. poll-notifications)
  local ENTRY="$2"       # Python function name (e.g. poll_notifications)
  local MEMORY="${3:-256Mi}"
  local TIMEOUT="${4:-540s}"

  echo ""
  echo "==> Deploying $NAME (entry: $ENTRY)..."
  gcloud functions deploy "$NAME" \
    --gen2 \
    --region="$REGION" \
    --runtime="$RUNTIME" \
    --source="$SOURCE" \
    --entry-point="$ENTRY" \
    --trigger-http \
    --no-allow-unauthenticated \
    --memory="$MEMORY" \
    --timeout="$TIMEOUT" \
    --env-vars-file="$ENV_FILE" \
    --set-secrets="$COMMON_SECRETS" \
    --project="$PROJECT" \
    --format=none
}

# ---- Deploy all functions ----

deploy_fn "poll-notifications"       "poll_notifications"      "512Mi" "120s"
deploy_fn "poll-inbound-dms"         "poll_inbound_dms"        "512Mi" "120s"
deploy_fn "execute-engagement-dms"   "execute_engagement_dms"  "512Mi" "300s"  # engagement DM queue: 10 items × 90–600s stagger
deploy_fn "scan-comment-targets"     "scan_comment_targets"    "512Mi" "540s"
deploy_fn "execute-comment"          "execute_comment"         "512Mi" "900s"   # includes up to 10-min jitter
deploy_fn "process-dm-queue"         "process_dm_queue"        "512Mi" "120s"
deploy_fn "execute-dm-batch"         "execute_dm_batch"        "512Mi" "3600s"  # up to 1hr for staggered sends
deploy_fn "follower-graph-prefetch"  "follower_graph_prefetch" "512Mi" "1800s"  # weekly — fetch + filter all fan profiles
deploy_fn "follower-graph-slot"      "follower_graph_slot"     "512Mi" "2700s"  # nightly — one 2000-fan slot (~35min max)
deploy_fn "follower-graph-score"     "follower_graph_score"    "512Mi" "120s"   # nightly — pure Firestore tier assignment
deploy_fn "starter-pack-discovery"   "starter_pack_discovery"  "512Mi" "1800s"
deploy_fn "cleanup-stale-docs"       "cleanup_stale_docs"      "512Mi" "300s"
deploy_fn "snapshot-follower-count"  "snapshot_follower_count" "256Mi" "60s"

echo ""
echo "All functions deployed."
echo "Run scheduler.sh to create/update Cloud Scheduler jobs."

# ---- Deploy dashboard API to Cloud Run ----
#
# Prerequisites:
#   gcloud secrets create dashboard-secret --replication-policy automatic
#   SECRET=$(openssl rand -hex 32)
#   echo -n "$SECRET" | gcloud secrets versions add dashboard-secret --data-file=-
#
# Build via Cloud Build (no local Docker needed):
#
# cat > /tmp/cloudbuild-dashboard.yaml <<'EOF'
# steps:
# - name: 'gcr.io/cloud-builders/docker'
#   args: ['build', '-t', 'gcr.io/sx-platform/sx-dashboard-api', '-f', 'bluesky/web/Dockerfile', '.']
# images: ['gcr.io/sx-platform/sx-dashboard-api']
# EOF
#
# gcloud builds submit --config /tmp/cloudbuild-dashboard.yaml --project=$PROJECT .
#
# DASHBOARD_IMAGE="gcr.io/$PROJECT/sx-dashboard-api"
# gcloud run deploy sx-dashboard-api \
#   --image="$DASHBOARD_IMAGE" \
#   --region="$REGION" \
#   --platform=managed \
#   --set-secrets="DASHBOARD_SECRET=dashboard-secret:latest,ANTHROPIC_API_KEY=anthropic-api-key:latest,BRANDVOICE_CONTENT=brandvoice-content:latest" \
#   --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT,FIRESTORE_DATABASE=sxplatformdatabase" \
#   --project="$PROJECT"
#
# Note: org policy blocks allUsers IAM binding. After deploy, run:
#   gcloud run services update sx-dashboard-api --region=$REGION --project=$PROJECT --no-invoker-iam-check
