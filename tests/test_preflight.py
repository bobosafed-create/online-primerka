import json
import pathlib
import tempfile
import unittest

from scripts import preflight


class PreflightTests(unittest.TestCase):
    def _valid_env(self, image_dir):
        return {
            "APP_ENV": "staging",
            "SITE_URL": "https://staging.example.test",
            "DATABASE_URL": "postgresql://app:password@db.example.test:5432/styleglobe",
            "ADMIN_PASSWORD": "a-long-admin-password",
            "SESSION_SECRET": "a-distinct-session-secret-with-at-least-32-bytes",
            "WAVESPEED_API_KEY": "configured",
            "YOOKASSA_SHOP_ID": "configured",
            "YOOKASSA_SECRET_KEY": "configured",
            "IMAGE_DIR": image_dir,
            "ENABLE_TEST_ORDERS": "0",
            "EMBEDDED_WORKER": "1",
            "TRUST_PROXY_HEADERS": "0",
        }

    def test_valid_staging_configuration_passes(self):
        with tempfile.TemporaryDirectory() as image_dir:
            results = preflight.evaluate(self._valid_env(image_dir))
        self.assertFalse(any(item["level"] == "error" for item in results))
        self.assertIn("configuration", {item["code"] for item in results})

    def test_production_rejects_test_orders_and_weak_secrets(self):
        with tempfile.TemporaryDirectory() as image_dir:
            env = self._valid_env(image_dir)
            env.update({
                "APP_ENV": "production",
                "SITE_URL": "http://example.test",
                "ADMIN_PASSWORD": "short",
                "SESSION_SECRET": "short",
                "ENABLE_TEST_ORDERS": "1",
            })
            results = preflight.evaluate(env)
        codes = {item["code"] for item in results if item["level"] == "error"}
        self.assertTrue({"site_url", "admin_password", "session_secret", "test_orders"} <= codes)

    def test_results_never_echo_secret_values(self):
        secret = "do-not-print-this-secret-value"
        results = preflight.evaluate({"SESSION_SECRET": secret}, check_paths=False)
        rendered = json.dumps(results, ensure_ascii=False)
        self.assertNotIn(secret, rendered)

    def test_required_schema_versions_match_application(self):
        source = pathlib.Path("main.py").read_text(encoding="utf-8")
        for version in preflight.REQUIRED_SCHEMA_VERSIONS:
            self.assertIn(f"_migration_done({version})", source)

    def test_ci_is_read_only_and_contains_no_deployment(self):
        source = pathlib.Path(".github/workflows/tests.yml").read_text(encoding="utf-8").lower()
        self.assertIn("contents: read", source)
        self.assertNotIn("deploy", source)
        self.assertNotIn("production", source)


if __name__ == "__main__":
    unittest.main()
