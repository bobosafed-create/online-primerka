import importlib
import os
import pathlib
import re
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.responses import Response


class RequestStub:
    def __init__(self, cookies=None, headers=None, ip="127.0.0.1"):
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.client = SimpleNamespace(host=ip)


class AdminSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ.update({
            "APP_ENV": "production",
            "DATABASE_URL": "",
            "SQLITE_PATH": str(pathlib.Path(cls.temp_dir.name) / "admin-test.db"),
            "SESSION_SECRET": "test-session-secret-that-is-long-enough",
            "SITE_URL": "https://example.test",
        })
        cls.app = importlib.import_module("main")
        cls.app.SQLITE_PATH = str(pathlib.Path(cls.temp_dir.name) / "admin-test.db")
        cls.app.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def setUp(self):
        self.old_password = self.app.ADMIN_PASSWORD
        self.app.ADMIN_PASSWORD = "correct horse battery staple"
        for table in ("admin_sessions", "rate_limits"):
            self.app.dbrun(f"DELETE FROM {table}")

    def tearDown(self):
        self.app.ADMIN_PASSWORD = self.old_password

    def _login(self):
        response = Response()
        result = self.app.admin_login(
            self.app.AdminLoginIn(password=self.app.ADMIN_PASSWORD),
            RequestStub(headers={"user-agent": "test-browser"}),
            response,
        )
        cookie_header = response.headers["set-cookie"]
        token = re.search(r"sg_admin=([^;]+)", cookie_header).group(1)
        return result, token, cookie_header

    def test_login_creates_persistent_secure_session_cookie(self):
        result, token, cookie_header = self._login()
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("Secure", cookie_header)
        self.assertIn("SameSite=strict", cookie_header)
        request = RequestStub(cookies={"sg_admin": token})
        session = self.app.require_admin(request)
        self.assertEqual(
            self.app._private_hash("admin-session", token),
            session["session_hash"],
        )
        self.assertTrue(result["csrfToken"])

    def test_admin_mutation_requires_csrf_and_logout_revokes_session(self):
        result, token, _ = self._login()
        without_csrf = RequestStub(cookies={"sg_admin": token})
        with self.assertRaises(self.app.HTTPException) as raised:
            self.app.admin_settings(without_csrf, self.app.SettingsIn(test_mode=False))
        self.assertEqual(403, raised.exception.status_code)

        authorized = RequestStub(
            cookies={"sg_admin": token},
            headers={"x-csrf-token": result["csrfToken"]},
        )
        self.assertTrue(self.app.admin_settings(authorized, self.app.SettingsIn(test_mode=False))["ok"])
        self.assertTrue(self.app.admin_logout(authorized, Response())["ok"])
        with self.assertRaises(self.app.HTTPException) as revoked:
            self.app.require_admin(without_csrf)
        self.assertEqual(401, revoked.exception.status_code)

    def test_session_resume_rotates_csrf_token(self):
        first, token, _ = self._login()
        resumed = self.app.admin_session(RequestStub(cookies={"sg_admin": token}))
        self.assertNotEqual(first["csrfToken"], resumed["csrfToken"])
        stale = RequestStub(cookies={"sg_admin": token}, headers={"x-csrf-token": first["csrfToken"]})
        with self.assertRaises(self.app.HTTPException) as raised:
            self.app.require_admin(stale, require_csrf=True)
        self.assertEqual(403, raised.exception.status_code)

    def test_database_rate_limit_is_atomic_across_threads(self):
        subject = uuid.uuid4().hex
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(
                lambda _: self.app.consume_rate_limit("test", subject, 5, 900),
                range(10),
            ))
        self.assertEqual(5, sum(item["allowed"] for item in results))
        self.assertEqual(list(range(1, 11)), sorted(item["hits"] for item in results))

    def test_proxy_ip_ignores_client_prepended_value(self):
        old_value = self.app.TRUST_PROXY_HEADERS
        self.app.TRUST_PROXY_HEADERS = True
        try:
            request = RequestStub(headers={"x-forwarded-for": "203.0.113.9, 198.51.100.7"})
            self.assertEqual("198.51.100.7", self.app._client_ip(request))
        finally:
            self.app.TRUST_PROXY_HEADERS = old_value

    def test_schema_migrations_are_versioned(self):
        rows = self.app.dbrun("SELECT version FROM schema_migrations ORDER BY version", (), "all")
        self.assertEqual([1, 2, 3, 4], [row["version"] for row in rows])

    def test_legacy_database_migrates_without_losing_orders_or_codes(self):
        old_path = self.app.SQLITE_PATH
        with tempfile.TemporaryDirectory() as legacy_dir:
            legacy_path = str(pathlib.Path(legacy_dir) / "legacy.db")
            conn = sqlite3.connect(legacy_path)
            conn.executescript("""
                CREATE TABLE codes(
                    code TEXT PRIMARY KEY, package TEXT, credits_total INTEGER,
                    credits_left INTEGER, has_video INTEGER, created_at TEXT
                );
                CREATE TABLE orders(
                    order_id TEXT PRIMARY KEY, package TEXT, amount TEXT, paid INTEGER,
                    is_test INTEGER, payment_id TEXT, code TEXT, created_at TEXT
                );
                INSERT INTO codes VALUES('SG-TEST-CODE','test',1,1,0,'2026-01-01');
                INSERT INTO orders VALUES('order-1','test','99.00',1,0,'payment-1','SG-TEST-CODE','2026-01-01');
            """)
            conn.commit()
            conn.close()
            try:
                self.app.SQLITE_PATH = legacy_path
                self.app.init_db()
                self.app.init_db()
                self.assertEqual(1, len(self.app.dbrun("SELECT * FROM orders", (), "all")))
                self.assertEqual(1, len(self.app.dbrun("SELECT * FROM codes", (), "all")))
                columns = self.app.dbrun("PRAGMA table_info(codes)", (), "all")
                self.assertIn("order_id", {row["name"] for row in columns})
                versions = self.app.dbrun("SELECT version FROM schema_migrations ORDER BY version", (), "all")
                self.assertEqual([1, 2, 3, 4], [row["version"] for row in versions])
            finally:
                self.app.SQLITE_PATH = old_path
                self.app.init_db()

    def test_admin_client_does_not_store_bearer_token(self):
        source = pathlib.Path("admin.html").read_text(encoding="utf-8")
        self.assertNotIn("X-Admin-Token", source)
        self.assertNotIn("localStorage", source)
        self.assertIn("X-CSRF-Token", source)
        self.assertIn("/api/admin/session", source)


if __name__ == "__main__":
    unittest.main()
