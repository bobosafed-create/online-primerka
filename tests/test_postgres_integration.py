import importlib
import os
import pathlib
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL", "").strip()


@unittest.skipUnless(POSTGRES_URL, "TEST_POSTGRES_URL is not configured")
class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parsed = urlparse(POSTGRES_URL)
        database_name = parsed.path.strip("/").lower()
        explicitly_allowed = os.getenv("ALLOW_POSTGRES_TEST_RESET") == "1"
        if not explicitly_allowed or "test" not in database_name:
            raise RuntimeError(
                "PostgreSQL integration tests require ALLOW_POSTGRES_TEST_RESET=1 "
                "and a database name containing 'test'"
            )
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ.update({
            "APP_ENV": "staging",
            "DATABASE_URL": POSTGRES_URL,
            "SESSION_SECRET": "postgres-integration-session-secret-32-bytes",
            "SITE_URL": "https://staging.example.test",
            "IMAGE_DIR": cls.temp_dir.name,
            "EMBEDDED_WORKER": "0",
        })
        cls.app = importlib.import_module("main")
        if not cls.app.USE_PG:
            raise RuntimeError("PostgreSQL integration test imported the SQLite backend")

    @classmethod
    def tearDownClass(cls):
        cls._truncate()
        cls.temp_dir.cleanup()

    @classmethod
    def _truncate(cls):
        cls.app.dbrun(
            "TRUNCATE TABLE generation_jobs,codes,orders,free_preview_usage,feedback,"
            "admin_sessions,rate_limits,settings,free_usage CASCADE"
        )

    def setUp(self):
        self._truncate()

    def test_all_schema_migrations_are_applied(self):
        rows = self.app.dbrun("SELECT version FROM schema_migrations ORDER BY version", (), "all")
        self.assertEqual([1, 2, 3, 4], [row["version"] for row in rows])

    def test_skip_locked_allows_only_one_worker_claim(self):
        code = self.app.create_code("test", 1, 0)
        job_id = uuid.uuid4().hex
        self.app.reserve_generation(code, "photo", job_id, uuid.uuid4().hex, 1, ["https://example.test/a"])
        with ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(
                lambda number: self.app.claim_generation_job(f"pg-worker-{number}"),
                range(12),
            ))
        claimed = [item for item in claims if item]
        self.assertEqual(1, len(claimed))
        self.assertEqual(job_id, claimed[0]["job_id"])

    def test_credit_reservation_and_refund_are_atomic(self):
        code = self.app.create_code("test", 1, 0)
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(
                lambda _: self.app.reserve_generation(code, "photo", uuid.uuid4().hex),
                range(10),
            ))
        accepted = [item for item in results if item]
        self.assertEqual(1, len(accepted))
        job = self.app.dbrun("SELECT * FROM generation_jobs", (), "one")
        self.assertTrue(self.app.finish_generation(job["job_id"], False, "integration test"))
        self.assertFalse(self.app.finish_generation(job["job_id"], False, "duplicate"))
        self.assertEqual(1, self.app.get_code(code)["credits_left"])

    def test_paid_order_issues_one_code_under_concurrency(self):
        order_id = uuid.uuid4().hex
        package = self.app.PACKAGE_BY_ID["pro"]
        self.app.dbrun(
            "INSERT INTO orders(order_id,package,amount,paid,is_test,created_at) VALUES(?,?,?,1,0,?)",
            (order_id, "pro", package["price"], self.app.now_iso()),
        )
        with ThreadPoolExecutor(max_workers=12) as pool:
            codes = list(pool.map(lambda _: self.app.issue_code_for_order(order_id), range(12)))
        self.assertEqual(1, len(set(codes)))
        rows = self.app.dbrun("SELECT * FROM codes WHERE order_id=?", (order_id,), "all")
        self.assertEqual(1, len(rows))

    def test_shared_rate_limit_is_atomic(self):
        subject = uuid.uuid4().hex
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(
                lambda _: self.app.consume_rate_limit("postgres-test", subject, 5, 900),
                range(12),
            ))
        self.assertEqual(5, sum(result["allowed"] for result in results))


if __name__ == "__main__":
    unittest.main()
