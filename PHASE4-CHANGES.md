# Staging readiness and CI — phase 4

This branch is intended for test-only CI validation. It must not be deployed automatically.

## Fail-closed preflight

`python scripts/preflight.py` validates staging/production configuration without printing secret values. It checks
HTTPS, PostgreSQL, secret strength and separation, payment/AI key presence, private storage and production test-mode
denial. `--check-database` also performs a read-only connection and verifies schema migration versions 1–5.

Migration 5 converts Unix timestamp fields from PostgreSQL `REAL` to `DOUBLE PRECISION`. This prevents newly queued
jobs from being delayed by the coarse precision of a 4-byte float at current epoch values.

## PostgreSQL integration suite

The opt-in PostgreSQL tests cover:

- schema migrations;
- `FOR UPDATE SKIP LOCKED` queue claims;
- concurrent final-credit reservation and idempotent refund;
- one access code per paid order under concurrency;
- shared database rate limiting.

The suite refuses to reset a database unless `ALLOW_POSTGRES_TEST_RESET=1` is explicit and the database name contains
`test`.

## Test-only GitHub Actions workflow

`.github/workflows/tests.yml` runs Python 3.13 unit tests and a PostgreSQL 16 service-container job. The workflow has
only `contents: read` permission and contains no deployment, package publishing or production credentials.

## Local limitation

Docker, PostgreSQL and protected staging keys were not available in the local workspace. The normal suite can be
run locally; the five PostgreSQL-specific tests are prepared and skipped until an isolated PostgreSQL test database
or the test-only CI job is available.
