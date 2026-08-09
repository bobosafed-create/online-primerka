# Security stabilization — phase 1

This package is a local development result. It has not been deployed.

## Fixed

- Stored XSS in the feedback section of the admin panel.
- Public test-order code issuance in production.
- Duplicate access-code issuance under concurrent payment callbacks.
- Concurrent overspending of the final photo/video credit.
- Lost credit after a failed generation.
- Untrusted, unlimited image data URLs.
- Wildcard CORS and missing baseline response headers.
- Client-controlled free-preview identity and concurrent preview reuse.
- Unbounded lifetime of temporary image files.
- Unpinned Python dependencies.
- Incorrect privacy statement claiming that images never leave the browser.
- YooKassa success accepted without binding payment ID, order ID, amount, currency and paid flag.
- YooKassa webhook trusted client-supplied metadata instead of the stored payment ID.
- Payment creation used an unrelated idempotency key and did not handle incomplete provider responses.

## Validation

Run:

```text
python -m unittest discover -s tests -v
python -m pip check
```

The included regression suite covers concurrent access-code issuance, atomic credit reservations,
refund idempotency, interrupted-job recovery, free-preview reservation, production test-order denial,
admin feedback escaping, feedback allowlisting, image decoding/re-encoding, YooKassa request binding,
payment verification, forged metadata rejection, and duplicate webhook idempotency.

## Deployment prerequisites

1. Review the database backup and migration plan.
2. Set all required production environment variables from `.env.example`.
3. Keep `APP_ENV=production`, `ENABLE_TEST_ORDERS=0`.
4. Use exactly one Uvicorn worker until an external queue is introduced.
5. Run tests against a staging PostgreSQL database and YooKassa sandbox.
6. Have the updated privacy wording reviewed before publication.
7. Deploy only after explicit owner approval and keep the previous release available for rollback.

## Still recommended for phase 2

- External durable generation queue and worker.
- Private object storage with authorized or signed downloads.
- Persistent server-side admin sessions and CSRF protection.
- Distributed rate limiting.
- PostgreSQL integration tests and a live YooKassa sandbox transaction on an isolated staging site.
- Alembic migrations and CI/CD.
