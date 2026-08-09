"""Fail-closed staging/production configuration preflight without printing secrets."""

import argparse
import json
import os
import sys
from urllib.parse import urlparse


REQUIRED_SCHEMA_VERSIONS = [1, 2, 3, 4, 5]


def _result(level, code, message):
    return {"level": level, "code": code, "message": message}


def evaluate(env=None, check_paths=True):
    env = dict(os.environ if env is None else env)
    results = []
    environment = env.get("APP_ENV", "").strip().lower()
    if environment not in {"staging", "production"}:
        results.append(_result("error", "app_env", "APP_ENV must be staging or production"))

    site_url = env.get("SITE_URL", "").strip()
    if not site_url.startswith("https://"):
        results.append(_result("error", "site_url", "SITE_URL must use HTTPS"))

    database_url = env.get("DATABASE_URL", "").strip()
    parsed_db = urlparse(database_url)
    if parsed_db.scheme not in {"postgres", "postgresql"} or not parsed_db.hostname or not parsed_db.path.strip("/"):
        results.append(_result("error", "database_url", "DATABASE_URL must identify a PostgreSQL database"))

    admin_password = env.get("ADMIN_PASSWORD", "")
    if len(admin_password) < 16:
        results.append(_result("error", "admin_password", "ADMIN_PASSWORD must contain at least 16 characters"))

    session_secret = env.get("SESSION_SECRET", "")
    if len(session_secret.encode()) < 32:
        results.append(_result("error", "session_secret", "SESSION_SECRET must contain at least 32 bytes"))
    if session_secret and session_secret == admin_password:
        results.append(_result("error", "secret_reuse", "SESSION_SECRET must differ from ADMIN_PASSWORD"))

    for name, code in (
        ("WAVESPEED_API_KEY", "wavespeed_key"),
        ("YOOKASSA_SHOP_ID", "yookassa_shop"),
        ("YOOKASSA_SECRET_KEY", "yookassa_key"),
    ):
        if not env.get(name, "").strip():
            results.append(_result("error", code, f"{name} is required"))

    image_dir = env.get("IMAGE_DIR", "").strip()
    if not image_dir or not os.path.isabs(image_dir):
        results.append(_result("error", "image_dir", "IMAGE_DIR must be an absolute private path"))
    elif check_paths and (not os.path.isdir(image_dir) or not os.access(image_dir, os.W_OK)):
        results.append(_result("error", "image_dir_access", "IMAGE_DIR must exist and be writable"))

    if environment == "production" and env.get("ENABLE_TEST_ORDERS", "0") == "1":
        results.append(_result("error", "test_orders", "ENABLE_TEST_ORDERS must be 0 in production"))
    if environment == "production" and env.get("EMBEDDED_WORKER", "1") != "1":
        results.append(_result("warning", "external_worker", "Confirm that a separate worker.py process is running"))
    if env.get("TRUST_PROXY_HEADERS", "0") == "1":
        results.append(_result("warning", "proxy_headers", "Confirm that only the trusted edge proxy reaches Uvicorn"))

    if not any(item["level"] == "error" for item in results):
        results.append(_result("ok", "configuration", "Configuration checks passed"))
    return results


def check_database(database_url):
    import psycopg2

    with psycopg2.connect(database_url, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
            versions = [row[0] for row in cursor.fetchall()]
            cursor.execute("SELECT 1")
    if versions != REQUIRED_SCHEMA_VERSIONS:
        return [_result("error", "schema_versions", "Database schema migrations are incomplete")]
    return [_result("ok", "database", "Database connection and schema checks passed")]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate StyleGlobe staging/production configuration")
    parser.add_argument("--check-database", action="store_true", help="connect and verify schema_migrations")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    results = evaluate()
    if args.check_database and not any(item["level"] == "error" for item in results):
        try:
            results.extend(check_database(os.environ["DATABASE_URL"]))
        except Exception:
            results.append(_result("error", "database_connection", "Database connection or schema check failed"))

    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    else:
        for item in results:
            print(f"[{item['level'].upper()}] {item['code']}: {item['message']}")
    return 1 if any(item["level"] == "error" for item in results) else 0


if __name__ == "__main__":
    sys.exit(main())
