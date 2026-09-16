# -*- coding: utf-8 -*-
"""בדיקות יחידה למנוע — לינק קצר, מקטעי SMS, תבנית ההודעה. הרצה: python -m unittest -v"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "unit-test-secret-not-prod")
os.environ["SEND_CHANNEL"] = "twilio"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine  # noqa: E402

DOC = "31115bf1-69f6-4c9b-a73d-533e2799aba1"


class ShortLinkTests(unittest.TestCase):
    def test_round_trip(self):
        tok = engine.short_token(DOC)
        self.assertEqual(len(tok), engine.SHORT_TOKEN_LEN)
        self.assertEqual(engine.resolve_short_token(tok), DOC)

    def test_link_shape(self):
        link = engine.short_link(DOC)
        self.assertTrue(link.startswith(engine.PUBLIC_BASE_URL + "/d/"))
        self.assertLessEqual(len(link), 70)

    def test_tamper_rejected(self):
        tok = engine.short_token(DOC)
        bad = tok[:-1] + ("A" if tok[-1] != "A" else "B")
        self.assertIsNone(engine.resolve_short_token(bad))
        flipped = ("A" if tok[0] != "A" else "B") + tok[1:]
        self.assertIsNone(engine.resolve_short_token(flipped))

    def test_garbage_rejected(self):
        for t in ["", "x", "x" * 29, "x" * 31, "../etc/passwd" + "x" * 17, None]:
            self.assertIsNone(engine.resolve_short_token(t))

    def test_non_uuid_falls_back(self):
        self.assertEqual(engine.short_link("not-a-uuid"), "")
        self.assertEqual(engine.short_link(""), "")

    def test_disabled_without_secret(self):
        old = os.environ.pop("SECRET_KEY")
        try:
            self.assertEqual(engine.short_link(DOC), "")
            self.assertIsNone(engine.resolve_short_token("x" * 30))
        finally:
            os.environ["SECRET_KEY"] = old


class SmsSegmentTests(unittest.TestCase):
    def test_gsm_boundaries(self):
        self.assertEqual(engine.sms_segments("a" * 160), 1)
        self.assertEqual(engine.sms_segments("a" * 161), 2)
        self.assertEqual(engine.sms_segments("a" * 306), 2)
        self.assertEqual(engine.sms_segments("a" * 307), 3)

    def test_extended_chars_count_double(self):
        self.assertEqual(engine.sms_segments("{" * 80), 1)
        self.assertEqual(engine.sms_segments("{" * 81), 2)

    def test_unicode_forces_ucs2(self):
        self.assertEqual(engine.sms_segments("שלום" * 17), 1)   # 68
        self.assertEqual(engine.sms_segments("שלום" * 18), 2)   # 72
        self.assertEqual(engine.sms_segments("a" * 100 + "₪"), 2)

    def test_real_message_is_one_segment_even_with_long_name(self):
        link = engine.short_link(DOC)
        msg = engine.build_message("MUKHAMMADJON ABDURAKHMONOVICH KHUDOYBERDIEV", "1234.56", "2026-09", link)
        self.assertEqual(engine.sms_segments(msg), 1, msg)
        self.assertLessEqual(len(msg), 160)

    def test_name_is_ascii_folded_and_capped(self):
        self.assertEqual(engine.sms_safe_name("José Ñandú"), "Jose Nandu")
        self.assertEqual(engine.sms_safe_name("סומצ'אי"), "Customer")
        self.assertEqual(engine.sms_safe_name("  Ivan   Petrov "), "Ivan Petrov")
        self.assertLessEqual(len(engine.sms_safe_name("A" * 90)), engine.SMS_NAME_MAX)

    def test_old_long_link_would_be_three_segments(self):
        long_link = "https://www.greeninvoice.co.il/api/v1/documents/download?d=" + "x" * 230
        msg = engine.build_message("KHUSEN MUKHAMEDOV", "36.3", "2026-07", long_link)
        self.assertEqual(engine.sms_segments(msg), 3)   # the 17/08 reality: 3 paid segments per worker

    def test_wave_estimate(self):
        rows = [{"name": f"Worker {i}", "amount": 55.0, "phone": "972500000000"} for i in range(142)]
        est = engine.sms_wave_estimate(rows, "2026-09")
        self.assertEqual(est["messages"], 142)
        self.assertEqual(est["segments"], 142)
        self.assertAlmostEqual(est["usd"], round(142 * engine.SMS_SEGMENT_COST_USD, 2))

    def test_whatsapp_channel_keeps_full_template(self):
        os.environ["SEND_CHANNEL"] = "whatsapp"
        try:
            msg = engine.build_message("Somchai", "55", "2026-09", "https://x/y")
            self.assertIn("שלום Somchai", msg)
        finally:
            os.environ["SEND_CHANNEL"] = "twilio"


if __name__ == "__main__":
    unittest.main()
