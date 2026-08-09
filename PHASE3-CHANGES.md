# Durable queue and signed storage — phase 3

This package is a local development result. It has not been pushed or deployed.

## Durable generation queue

- The in-memory `TASKS` dictionary and one-thread-per-request model were removed.
- Paid and free-preview jobs are persisted in `generation_jobs` before the HTTP request returns.
- Workers claim queued jobs atomically; two workers cannot process the same queued row.
- Queued jobs survive web-process restarts.
- Worker heartbeats distinguish active work from abandoned processing jobs.
- Abandoned paid work refunds its credit exactly once; queued work is not refunded or lost.
- `worker.py` supports a separate process. `EMBEDDED_WORKER=1` keeps a low-cost single-service mode.

## Private file delivery

- Uploads, previews, photos and videos are no longer exposed through permanent public paths.
- Each file URL carries an HMAC signature and expiry timestamp.
- Invalid, modified and expired links return HTTP 403.
- WaveSpeed results are downloaded through a streaming size limit before being exposed.
- Video results are stored locally instead of returning the provider URL to the customer.
- `IMAGE_DIR` allows web and worker processes to share a private persistent volume.

## Schema

Migration 4 adds queue payload, claim, attempt and heartbeat fields without discarding existing jobs.

## Remaining infrastructure step

The included local storage backend is suitable only when web and worker share one private filesystem.
Multi-host deployment still requires an S3-compatible private bucket with short-lived signed URLs.
