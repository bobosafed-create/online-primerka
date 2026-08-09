# Security stabilization — phase 2

This package is a local development result. It has not been pushed or deployed.

## Administrative security

- Login creates a random server-side session stored only as a keyed hash in the database.
- The browser receives an expiring `HttpOnly` cookie; JavaScript never receives or stores the session secret.
- Every state-changing admin request requires a separate rotating CSRF token.
- Refreshing the admin page resumes a valid session and rotates its CSRF token.
- Logout deletes the session from the database and expires the cookie.
- Sensitive admin API responses use `Cache-Control: no-store`.

## Shared rate limiting

Admin login attempt counters now use the `rate_limits` database table and an atomic upsert. Limits therefore
survive application restarts and are shared between application processes that use the same PostgreSQL database.

## Versioned schema

The `schema_migrations` table records ordered migrations. Existing code/order data is retained while the phase 1
and phase 2 columns, indexes and tables are added. A migration failure stops production startup instead of running
with a partially protected schema.

## Validation

Run:

```text
python -m unittest discover -s tests -v
python -m pip check
```

The phase 2 tests cover secure cookie attributes, persistent sessions, CSRF denial, CSRF rotation, logout
revocation, concurrent shared rate limiting, migration versioning and removal of the JavaScript bearer token.
