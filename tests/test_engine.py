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


class _FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.ok = payload, status, status < 400

    def json(self):
        return self._p

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class MonthDocsAndRunTests(unittest.TestCase):
    def setUp(self):
        self._orig = {}

    def patch(self, mod, name, value):
        self._orig[(mod, name)] = getattr(mod, name)
        setattr(mod, name, value)

    def tearDown(self):
        for (mod, name), v in self._orig.items():
            setattr(mod, name, v)

    def _items(self):
        return [{"id": DOC, "number": "61856", "url": {"origin": "https://www.greeninvoice.co.il/x/1", "he": "h"},
                 "client": {"phone": "050-1234567"}, "remarks": "חיוב\nדרכון: N1 · טלפון: 0501234567"},
                {"id": "815cfa0c-c886-4d76-94a4-96e40c87a0e9", "number": "61855", "url": {"origin": "https://www.greeninvoice.co.il/x/2"},
                 "client": {"phone": "0522345678"}, "remarks": "בלי דרכון"}]

    def test_gi_month_docs_maps_phone_and_passport(self):
        self.patch(engine.requests, "post", lambda *a, **k: _FakeResp({"items": self._items()}))
        d = engine.gi_month_docs({}, "2026-09")
        self.assertEqual(d["phones"]["972501234567"]["number"], "61856")
        self.assertEqual(d["phones"]["972501234567"]["url"], "https://www.greeninvoice.co.il/x/1")
        self.assertEqual(d["passports"]["N1"]["id"], DOC)
        self.assertIn("972522345678", d["phones"])
        self.assertEqual(set(d["passports"]), {"N1"})
        phones, passports = engine.gi_billed({}, "2026-09")
        self.assertEqual(phones, {"972501234567", "972522345678"})

    def _common(self, sent):
        self.patch(engine, "gi_token", lambda: {})
        self.patch(engine, "gi_verify_business", lambda h: "ok")
        self.patch(engine.requests, "post", lambda *a, **k: _FakeResp({"items": self._items()}))
        self.patch(engine, "send_message", lambda phone, text: sent.append((phone, text)) or "SMS")

    def test_messages_only_sends_to_existing_docs_and_never_creates(self):
        sent, created = [], []
        self._common(sent)
        self.patch(engine, "gi_create_doc", lambda *a, **k: created.append(1) or (DOC, "x", "u"))
        rows = [{"passport": "N1", "name": "Somchai", "phone": "972501234567", "amount": 55.0},
                {"passport": "N9", "name": "Nobody", "phone": "972509999999", "amount": 10.0}]
        st = engine.RunState(run_id="r", month="2026-09")
        engine.execute_run(st, rows, "2026-09", messages_only=True)
        self.assertEqual(st.status, "finished", st.error)
        self.assertTrue(st.messages_only)
        self.assertEqual(created, [])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "972501234567")
        self.assertIn(engine.short_token(DOC), sent[0][1])          # הלינק הקצר של המסמך הקיים
        self.assertEqual(st.sent_count, 1)
        self.assertEqual(st.results[0]["doc_number"], "61856")
        self.assertEqual([s["reason"] for s in st.skipped], ["אין מסמך לחודש הזה — צריך הפקה רגילה"])

    def test_normal_run_marks_unsent_when_send_fails(self):
        self.patch(engine, "gi_token", lambda: {})
        self.patch(engine, "gi_verify_business", lambda h: "ok")
        self.patch(engine.requests, "post", lambda *a, **k: _FakeResp({"items": []}))
        self.patch(engine, "gi_create_doc", lambda h, r, m: ("9f000000-0000-4000-8000-000000000001", "700", "https://gi/x"))

        def boom(phone, text):
            raise RuntimeError("insufficient funds")
        self.patch(engine, "send_message", boom)
        st = engine.RunState(run_id="r", month="2026-09")
        engine.execute_run(st, [{"passport": "N1", "name": "A", "phone": "972501234567", "amount": 55.0}], "2026-09")
        self.assertEqual(st.status, "finished", st.error)
        self.assertEqual(st.ok_count, 1)
        self.assertEqual(st.sent_count, 0)
        self.assertEqual(len(st.unsent), 1)
        self.assertTrue(st.results[0]["delivery"].startswith(engine.FAILED_PREFIX))

    def test_normal_run_skips_existing_docs(self):
        sent = []
        self._common(sent)
        self.patch(engine, "gi_create_doc", lambda h, r, m: ("9f000000-0000-4000-8000-000000000002", "701", "https://gi/y"))
        rows = [{"passport": "N1", "name": "Somchai", "phone": "972501234567", "amount": 55.0},
                {"passport": "N9", "name": "New", "phone": "972509999999", "amount": 10.0}]
        st = engine.RunState(run_id="r", month="2026-09")
        engine.execute_run(st, rows, "2026-09")
        self.assertEqual([s["name"] for s in st.skipped], ["Somchai"])
        self.assertEqual([r["name"] for r in st.results], ["New"])
        self.assertEqual(st.sent_count, 1)


if __name__ == "__main__":
    unittest.main()
