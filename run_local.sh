#!/bin/bash
# Start local dashboard API server with GCP credential check

if ! gcloud auth application-default print-access-token &>/dev/null; then
  echo "GCP credentials expired — re-authenticating..."
  gcloud auth application-default login
fi

set -a && source bluesky/reply/.env && set +a

GOOGLE_CLOUD_PROJECT=sx-platform \
FIRESTORE_DATABASE=sxplatformdatabase \
  uvicorn bluesky.web.api.main:app --port 8000 &

UVICORN_PID=$!
trap "kill -9 $UVICORN_PID 2>/dev/null; exit 0" INT TERM

sleep 2 && open bluesky/web/dashboard.html

wait $UVICORN_PID
