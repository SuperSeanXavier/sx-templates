# Deploy Hotfixes Log

Issues encountered during first deploy of sx-platform Cloud Functions (2026-03-27).
Each item needs to be either fixed in code/deploy.sh or documented in a post-deploy README for the Peachy Creator Kit template.

---

## 1. Firestore index — `function_runs` collection

**Symptom:** Any query filtering by `function` AND `run_at` fails at runtime.
**Fix:** Create composite index manually after deploy:
```bash
gcloud firestore indexes composite create \
  --collection-group=function_runs \
  --field-config=field-path=function,order=ASCENDING \
  --field-config=field-path=run_at,order=ASCENDING \
  --database=<DATABASE_ID> \
  --project=<PROJECT_ID>
```
**Template action:** Add to post-deploy checklist. Could potentially be automated via `firestore.indexes.json` + `firebase deploy --only firestore:indexes`.

---

## 2. Firestore index — `comment_queue` collection

**Symptom:** `execute-comment` fails with `FailedPrecondition: The query requires an index` on `comment_queue` (filters `status`, sorts `posted_at`).
**Fix:** Create composite index manually after deploy:
```bash
gcloud firestore indexes composite create \
  --collection-group=comment_queue \
  --field-config=field-path=status,order=ASCENDING \
  --field-config=field-path=posted_at,order=ASCENDING \
  --database=<DATABASE_ID> \
  --project=<PROJECT_ID>
```
**Template action:** Same as above — bundle all required indexes into a `firestore.indexes.json` so they deploy automatically.

---

## 3. IAM — compute SA missing `roles/datastore.user`

**Symptom:** All functions fail with `PERMISSION_DENIED` on Firestore reads/writes.
**Fix:**
```bash
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com" \
  --role="roles/datastore.user"
```
**Template action:** Add to deploy.sh as a preflight step, or document in post-deploy checklist.

---

## 4. IAM — compute SA missing `roles/run.invoker`

**Symptom:** Cloud Scheduler jobs fail with status code 7 (PERMISSION_DENIED) — scheduler cannot invoke Cloud Run-backed functions.
**Fix:**
```bash
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com" \
  --role="roles/run.invoker"
```
**Template action:** Same as above — add to deploy.sh preflight or post-deploy checklist.

---

## 5. scheduler.sh — wrong service account

**Symptom:** `bash scheduler.sh` fails with `NOT_FOUND` — script was using `@appspot.gserviceaccount.com` (App Engine default SA) which doesn't exist in projects that don't use App Engine.
**Fix:** Changed SA in `scheduler.sh` to use compute SA:
```bash
SA="$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
```
**Template action:** Already fixed in code.

---

## 6. deploy.sh — `--format=none` missing

**Symptom:** After each function deploy, gcloud prints a full YAML block describing the deployed function — very verbose with 11 functions.
**Fix:** Added `--format=none` to the `gcloud functions deploy` call in `deploy.sh`.
**Template action:** Already fixed in code.

---

## 7. Memory limits too low (256Mi → 512Mi)

**Symptom:** `poll-notifications`, `poll-inbound-dms`, `scan-comment-targets`, `execute-comment`, `process-dm-queue`, `follower-graph-score`, `cleanup-stale-docs` all OOM at 256Mi. Actual usage was 280–310 MiB.
**Fix:** Bumped all functions to 512Mi in `deploy.sh`.
**Template action:** Already fixed in code.

---

## 8. `follower-graph-prefetch` must be run manually on first deploy

**Symptom:** Nightly slot jobs (`follower-graph-slot-0` through `-4`) have no data to process until prefetch has run at least once. The scheduler only fires it weekly (Saturday 1am).
**Fix:** Run manually after first deploy:
```bash
gcloud functions call follower-graph-prefetch \
  --gen2 \
  --region=<REGION> \
  --project=<PROJECT_ID>
```
**Template action:** Document in post-deploy checklist.

---

## 9. Session restore error — `import_session_string` attribute missing

**Symptom:** Functions log `[auth] session restore failed ('Client' object has no attribute 'import_session_string'), falling back to full login`. Functions still work but burn a `createSession` quota call each time.
**Status:** Workaround in place (falls back to full login). Root cause: atproto SDK version mismatch with the session persistence implementation in `bluesky_client.py`.
**Template action:** Fix `bluesky_client.py` session restore to use correct SDK method before exporting as template.

---

## Summary — items still needing code fixes before template export

| # | Issue | Status |
|---|---|---|
| 1 | `function_runs` index | Needs `firestore.indexes.json` |
| 2 | `comment_queue` index | Needs `firestore.indexes.json` |
| 3 | `roles/datastore.user` IAM grant | Needs deploy.sh preflight |
| 4 | `roles/run.invoker` IAM grant | Needs deploy.sh preflight |
| 9 | Session restore SDK mismatch | Needs `bluesky_client.py` fix |

Items 5, 6, 7 are already fixed in code.
Item 8 (manual prefetch) is inherently a one-time manual step — document in README.

---

## Future improvement — parallel deploys

`deploy.sh` currently deploys all 11 functions sequentially (~20-25 min total).
Running them in parallel with `&` + `wait` would cut this to ~2-3 min (slowest single deploy).
Blocked on: the temp env file approach — the file path must be stable for parallel subshells to reference it.
Fix: write the env file to a fixed path (e.g. `/tmp/sx-deploy-env.yaml`) instead of a mktemp path, then parallelize the `deploy_fn` calls with `&` and add `wait` at the end.
