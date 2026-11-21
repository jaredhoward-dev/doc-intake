"""Smoke tests: the package imports, configure() refuses unknown settings, the detectors count and redact."""
import unittest

import doc_intake
from doc_intake import detect

CARD = "4111 1111 1111 1111"  # a well-known test card number (passes Luhn), not a real account


class Smoke(unittest.TestCase):
    def test_configure_rejects_unknown_settings(self):
        with self.assertRaises(TypeError):
            doc_intake.configure(no_such_setting=1)

    def test_card_is_counted_and_redacted(self):
        text = f"Paid with card {CARD} on 2030-01-02"
        c = detect.counts(detect.find_spans(text))
        self.assertEqual(c.get("card"), 1)
        red = detect.redact(text)[0]
        self.assertNotIn(CARD, red)
        self.assertIn("2030-01-02", red)

    def test_plain_text_has_no_findings(self):
        self.assertEqual(detect.counts(detect.find_spans("Total due 12.34 on 2030-01-02")), {})


if __name__ == "__main__":
    unittest.main()
