import importlib
import base64
import io
import os
import pathlib
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from PIL import Image


class SecurityRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ.update({
            "APP_ENV": "production",
            "DATABASE_URL": "",
            "SQLITE_PATH": str(pathlib.Path(cls.temp_dir.name) / "test.db"),
            "SESSION_SECRET": "test-session-secret-that-is-long-enough",
            "SITE_URL": "https://example.test",
            "ENABLE_TEST_ORDERS": "1",
            "TEST_ORDER_SECRET": "must-not-work-in-production",
        })
        cls.app = importlib.import_module("main")

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def setUp(self):
        for table in ("generation_jobs", "codes", "orders", "free_preview_usage", "feedback"):
            self.app.dbrun(f"DELETE FROM {table}")

    def _paid_order(self, package="pro"):
        order_id = uuid.uuid4().hex
        pkg = self.app.PACKAGE_BY_ID[package]
        self.app.dbrun(
            "INSERT INTO orders(order_id,package,amount,paid,is_test,created_at) VALUES(?,?,?,1,0,?)",
            (order_id, package, pkg["price"], self.app.now_iso()),
        )
        return order_id

    def test_one_paid_order_gets_exactly_one_code_under_concurrency(self):
        order_id = self._paid_order()
        with ThreadPoolExecutor(max_workers=20) as pool:
            codes = list(pool.map(lambda _: self.app.issue_code_for_order(order_id), range(20)))
        self.assertEqual(1, len(set(codes)))
        rows = self.app.dbrun("SELECT * FROM codes WHERE order_id=?", (order_id,), "all")
        self.assertEqual(1, len(rows))

    def test_one_credit_cannot_start_two_jobs(self):
        code = self.app.create_code("test", 1, 0)
        barrier = threading.Barrier(2)

        def reserve(_):
            barrier.wait()
            return self.app.reserve_generation(code, "photo", uuid.uuid4().hex)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(1, sum(result is not None for result in results))
        self.assertEqual(0, self.app.get_code(code)["credits_left"])

    def test_failed_generation_refunds_exactly_once(self):
        code = self.app.create_code("test", 1, 0)
        job_id = uuid.uuid4().hex
        self.assertIsNotNone(self.app.reserve_generation(code, "photo", job_id))
        self.assertTrue(self.app.finish_generation(job_id, False, "provider timeout"))
        self.assertFalse(self.app.finish_generation(job_id, False, "duplicate callback"))
        self.assertEqual(1, self.app.get_code(code)["credits_left"])

    def test_interrupted_generation_is_refunded_on_recovery(self):
        code = self.app.create_code("test", 1, 0)
        job_id = uuid.uuid4().hex
        self.assertIsNotNone(self.app.reserve_generation(code, "photo", job_id))
        self.assertEqual(1, self.app.recover_interrupted_generations())
        self.assertEqual(1, self.app.get_code(code)["credits_left"])

    def test_free_preview_session_is_reserved_once(self):
        session_hash = "session-1"
        ip_hash = "ip-1"
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: self.app.reserve_free_preview(session_hash, ip_hash), range(8)
            ))
        self.assertEqual(1, sum(results))

    def test_production_test_orders_are_disabled(self):
        class Request:
            headers = {"x-test-order-secret": "must-not-work-in-production"}

        with self.assertRaises(self.app.HTTPException) as raised:
            self.app.test_order(self.app.TestOrderIn(package="pro"), Request())
        self.assertEqual(404, raised.exception.status_code)

    def test_feedback_reason_has_server_allowlist(self):
        with self.assertRaises(self.app.HTTPException):
            self.app.submit_feedback(self.app.FeedbackIn(
                reason='<img src=x onerror="alert(1)">', comment="x"
            ))

    def test_admin_feedback_uses_text_nodes(self):
        source = pathlib.Path("admin.html").read_text(encoding="utf-8")
        self.assertIn("appendTextCell(row,x.reason", source)
        self.assertIn("appendTextCell(row,x.comment", source)
        self.assertNotIn("fbtb.innerHTML", source)

    def test_image_upload_is_decoded_and_reencoded(self):
        image = Image.new("RGBA", (16, 16), (255, 0, 0, 120))
        payload = io.BytesIO()
        image.save(payload, "PNG")
        data_url = "data:image/png;base64," + base64.b64encode(payload.getvalue()).decode()
        url = self.app._save_dataurl(data_url)
        token = url.rsplit("/", 1)[-1]
        path = pathlib.Path(self.app.IMG_DIR) / f"{token}.jpg"
        try:
            self.assertTrue(path.exists())
            with Image.open(path) as saved:
                self.assertEqual("JPEG", saved.format)
                self.assertEqual("RGB", saved.mode)
        finally:
            path.unlink(missing_ok=True)

    def test_fake_image_is_rejected(self):
        fake = "data:image/jpeg;base64," + base64.b64encode(b"not an image").decode()
        with self.assertRaises(ValueError):
            self.app._save_dataurl(fake)


if __name__ == "__main__":
    unittest.main()
