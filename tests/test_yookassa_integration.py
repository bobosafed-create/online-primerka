import asyncio
import importlib
import os
import pathlib
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch


class YooKassaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ.update({
            "APP_ENV": "production",
            "DATABASE_URL": "",
            "SQLITE_PATH": str(pathlib.Path(cls.temp_dir.name) / "yookassa-test.db"),
            "SESSION_SECRET": "test-session-secret-that-is-long-enough",
            "SITE_URL": "https://example.test",
        })
        cls.app = importlib.import_module("main")
        cls.app.SQLITE_PATH = str(pathlib.Path(cls.temp_dir.name) / "yookassa-test.db")
        cls.app.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def setUp(self):
        for table in ("generation_jobs", "codes", "orders"):
            self.app.dbrun(f"DELETE FROM {table}")
        self.old_shop_id = self.app.YOOKASSA_SHOP_ID
        self.old_secret = self.app.YOOKASSA_SECRET_KEY
        self.app.YOOKASSA_SHOP_ID = "sandbox-shop"
        self.app.YOOKASSA_SECRET_KEY = "sandbox-secret"
        self.app._YK = True

    def tearDown(self):
        self.app.YOOKASSA_SHOP_ID = self.old_shop_id
        self.app.YOOKASSA_SECRET_KEY = self.old_secret

    def _pending_order(self, package="pro", payment_id="pay-1"):
        order_id = uuid.uuid4().hex
        pkg = self.app.PACKAGE_BY_ID[package]
        self.app.dbrun(
            "INSERT INTO orders(order_id,package,amount,paid,is_test,payment_id,created_at) "
            "VALUES(?,?,?,0,0,?,?)",
            (order_id, package, pkg["price"], payment_id, self.app.now_iso()),
        )
        return self.app.dbrun("SELECT * FROM orders WHERE order_id=?", (order_id,), "one")

    def _payment(self, order, **changes):
        values = {
            "id": order["payment_id"],
            "status": "succeeded",
            "paid": True,
            "metadata": {"order_id": order["order_id"]},
            "amount": SimpleNamespace(value=order["amount"], currency=self.app.CURRENCY),
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def test_create_payment_binds_amount_order_and_idempotency_key(self):
        captured = {}

        def fake_create(payload, idempotency_key=None):
            captured["payload"] = payload
            captured["key"] = idempotency_key
            return SimpleNamespace(
                id="sandbox-payment-1",
                confirmation=SimpleNamespace(confirmation_url="https://sandbox.test/pay/1"),
            )

        with patch.object(self.app.Payment, "create", side_effect=fake_create):
            result = self.app.create_payment(self.app.CreatePaymentIn(package="test"))

        order = self.app.dbrun("SELECT * FROM orders WHERE order_id=?", (result["order_id"],), "one")
        self.assertEqual(result["order_id"], captured["key"])
        self.assertEqual(result["order_id"], captured["payload"]["metadata"]["order_id"])
        self.assertEqual("99.00", captured["payload"]["amount"]["value"])
        self.assertEqual(self.app.CURRENCY, captured["payload"]["amount"]["currency"])
        self.assertEqual("sandbox-payment-1", order["payment_id"])

    def test_matching_succeeded_payment_issues_exactly_one_code(self):
        order = self._pending_order()
        payment = self._payment(order)
        with patch.object(self.app.Payment, "find_one", return_value=payment):
            first = self.app.verify_and_issue(order)
            refreshed = self.app.dbrun("SELECT * FROM orders WHERE order_id=?", (order["order_id"],), "one")
            second = self.app.verify_and_issue(refreshed)

        self.assertTrue(first[0])
        self.assertEqual(first[1], second[1])
        codes = self.app.dbrun("SELECT * FROM codes WHERE order_id=?", (order["order_id"],), "all")
        self.assertEqual(1, len(codes))

    def test_mismatched_payment_never_issues_code(self):
        cases = (
            {"paid": False},
            {"id": "another-payment"},
            {"metadata": {"order_id": "another-order"}},
            {"amount": SimpleNamespace(value="1.00", currency=self.app.CURRENCY)},
            {"amount": SimpleNamespace(value="3990.00", currency="USD")},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.app.dbrun("DELETE FROM codes")
                self.app.dbrun("DELETE FROM orders")
                order = self._pending_order()
                with patch.object(self.app.Payment, "find_one", return_value=self._payment(order, **changes)):
                    paid, code = self.app.verify_and_issue(order)
                self.assertFalse(paid)
                self.assertIsNone(code)
                self.assertEqual(0, len(self.app.dbrun("SELECT * FROM codes", (), "all")))

    def test_webhook_uses_stored_payment_id_not_untrusted_metadata(self):
        order = self._pending_order(payment_id="pay-real")
        payment = self._payment(order)

        class Request:
            async def json(self):
                return {
                    "event": "payment.succeeded",
                    "object": {"id": "pay-real", "metadata": {"order_id": "forged-order"}},
                }

        with patch.object(self.app.Payment, "find_one", return_value=payment):
            response = asyncio.run(self.app.yookassa_webhook(Request()))

        self.assertEqual(200, response.status_code)
        refreshed = self.app.dbrun("SELECT * FROM orders WHERE order_id=?", (order["order_id"],), "one")
        self.assertEqual(1, refreshed["paid"])
        self.assertIsNotNone(refreshed["code"])


if __name__ == "__main__":
    unittest.main()
