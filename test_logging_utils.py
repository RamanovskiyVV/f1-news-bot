import logging
import unittest

from logging_utils import SecretRedactionFilter, redact_secrets


class SecretRedactionTests(unittest.TestCase):
    def test_redacts_tokens_and_query_credentials(self):
        message = (
            "POST https://api.telegram.org/bot123456:abc_DEF/getUpdates "
            "https://example.test/search?key=google-secret&api_key=another "
            "sk-example-secret"
        )
        safe = redact_secrets(message)
        self.assertNotIn("123456:abc_DEF", safe)
        self.assertNotIn("google-secret", safe)
        self.assertNotIn("another", safe)
        self.assertNotIn("sk-example-secret", safe)
        self.assertGreaterEqual(safe.count("[REDACTED]"), 4)

    def test_filter_renders_arguments_before_redaction(self):
        record = logging.LogRecord(
            "httpx",
            logging.INFO,
            __file__,
            1,
            "request %s",
            ("https://api.telegram.org/bot123:secret/getUpdates",),
            None,
        )
        self.assertTrue(SecretRedactionFilter().filter(record))
        self.assertEqual(record.args, ())
        self.assertNotIn("123:secret", record.getMessage())


if __name__ == "__main__":
    unittest.main()
