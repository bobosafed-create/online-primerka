import importlib
import os
import pathlib
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from PIL import Image


class QueueAndStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ.update({
            "APP_ENV": "production",
            "DATABASE_URL": "",
            "SQLITE_PATH": str(pathlib.Path(cls.temp_dir.name) / "queue-test.db"),
            "SESSION_SECRET": "test-session-secret-that-is-long-enough",
            "SITE_URL": "https://example.test",
            "EMBEDDED_WORKER": "0",
        })
        cls.app = importlib.import_module("main")
        cls.app.SQLITE_PATH = str(pathlib.Path(cls.temp_dir.name) / "queue-test.db")
        cls.app.EMBEDDED_WORKER = False
        cls.app.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def setUp(self):
        for table in ("generation_jobs", "codes", "free_preview_usage"):
            self.app.dbrun(f"DELETE FROM {table}")

    def _queued_photo(self):
        code = self.app.create_code("test", 1, 0)
        job_id = uuid.uuid4().hex
        token = uuid.uuid4().hex
        balance = self.app.reserve_generation(
            code, "photo", job_id, token, 1,
            [self.app._signed_file_url("upload", uuid.uuid4().hex)],
        )
        self.assertIsNotNone(balance)
        return code, job_id, token

    def test_queued_job_is_persistent_and_not_refunded_on_recovery(self):
        code, job_id, _ = self._queued_photo()
        row = self.app.dbrun("SELECT * FROM generation_jobs WHERE job_id=?", (job_id,), "one")
        self.assertEqual("queued", row["status"])
        self.assertEqual(0, self.app.recover_interrupted_generations(stale_seconds=1))
        self.assertEqual(0, self.app.get_code(code)["credits_left"])

    def test_only_one_worker_can_claim_a_job(self):
        _, job_id, _ = self._queued_photo()
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(
                lambda number: self.app.claim_generation_job(f"worker-{number}"),
                range(8),
            ))
        claimed = [item for item in claims if item]
        self.assertEqual(1, len(claimed))
        self.assertEqual(job_id, claimed[0]["job_id"])

    def test_worker_result_and_status_survive_outside_process_memory(self):
        code, job_id, token = self._queued_photo()
        job = self.app.claim_generation_job("worker-test")

        def fake_download(_url, path):
            Image.new("RGB", (8, 8), (20, 40, 60)).save(path, "JPEG")

        with patch.object(self.app, "_generate", return_value="https://provider.test/result.jpg"), \
             patch.object(self.app, "_download_to", side_effect=fake_download):
            self.app.process_generation_job(job, "worker-test")

        status = self.app.task_status(job_id)
        self.assertEqual("done", status["status"])
        self.assertEqual("photo", status["kind"])
        self.assertTrue(self.app._private_file_path("photo", token))
        self.assertEqual(0, self.app.get_code(code)["credits_left"])
        pathlib.Path(self.app._private_file_path("photo", token)).unlink(missing_ok=True)

    def test_signed_file_link_rejects_tampering_and_expiry(self):
        token = uuid.uuid4().hex
        path = pathlib.Path(self.app._private_file_path("upload", token))
        Image.new("RGB", (4, 4), (1, 2, 3)).save(path, "JPEG")
        try:
            url = self.app._signed_file_url("upload", token, int(time.time()) + 60)
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            response = self.app.serve_private_file(
                "upload", token, int(query["exp"][0]), query["sig"][0]
            )
            self.assertEqual(str(path), response.path)
            with self.assertRaises(self.app.HTTPException) as tampered:
                self.app.serve_private_file("upload", token, int(query["exp"][0]), "0" * 64)
            self.assertEqual(403, tampered.exception.status_code)

            expired = int(time.time()) - 1
            with self.assertRaises(self.app.HTTPException) as old:
                self.app.serve_private_file(
                    "upload", token, expired, self.app._file_signature("upload", token, expired)
                )
            self.assertEqual(403, old.exception.status_code)
        finally:
            path.unlink(missing_ok=True)

    def test_source_no_longer_uses_in_memory_task_dictionary(self):
        source = pathlib.Path("main.py").read_text(encoding="utf-8")
        self.assertNotIn("TASKS =", source)
        self.assertNotIn("TASKS[", source)
        self.assertIn("claim_generation_job", source)


if __name__ == "__main__":
    unittest.main()
