# -*- coding: utf-8 -*-
"""בדיקות ראוטים עם Flask test client — הלינק הקצר ושומר היתרה. בלי רשת: חשבונית ירוקה
ו-Twilio מוחלפים בפונקציות מזויפות. הרצה: python -m unittest -v"""
import os
import sys
import unittest
from pathlib import Path

os.environ["SECRET_KEY"] = "unit-test-secret-not-prod"
os.environ["SEND_CHANNEL"] = "twilio"
os.environ["COOKIE_SECURE"] = "0"
os.environ.pop("RENDER", None)          # בלי keep-alive בבדיקות
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as app_module  # noqa: E402
import engine  # noqa: E402

DOC = "31115bf1-69f6-4c9b-a73d-533e2799aba1"
ROWS = [{"passport": "N1", "name": "Somchai Prasert", "phone": "972501234567", "amount": 55.0, "gmt": ""},
        {"passport": "N2", "name": "Ivan Petrov", "phone": "972522345678", "amount": 60.5, "gmt": ""}]


class _Base(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self._orig = {}
        self.patch(app_module, "gi_headers_cached", lambda force=False: {"Authorization": "Bearer fake"})
        self.patch(engine, "twilio_balance", lambda: 10.0)
        self.calls = []
        self.patch(engine, "execute_run", lambda state, rows, month, limit=0: self.calls.append((len(rows), limit)))
        self.patch(app_module, "notify_telegram", lambda text: None)
        app_module.PENDING.clear()
        app_module.RUNS.clear()

    def patch(self, mod, name, value):
        self._orig[(mod, name)] = getattr(mod, name)
        setattr(mod, name, value)

    def tearDown(self):
        for (mod, name), v in self._orig.items():
            setattr(mod, name, v)

    def login(self, role="manager"):
        with self.client.session_transaction() as s:
            s["role"] = role
            s["_csrf"] = "csrf-test"


class ShortLinkRouteTests(_Base):
    def test_valid_token_redirects_to_green_invoice(self):
        self.patch(engine, "gi_doc_url", lambda headers, doc_id: f"https://www.greeninvoice.co.il/api/v1/documents/download?d=SIGNED-{doc_id}")
        r = self.client.get("/d/" + engine.short_token(DOC))
        self.assertEqual(r.status_code, 302)
        self.assertIn(DOC, r.headers["Location"])
        self.assertTrue(r.headers["Location"].startswith("https://www.greeninvoice.co.il/"))
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_public_no_login_needed(self):
        self.patch(engine, "gi_doc_url", lambda headers, doc_id: "https://www.greeninvoice.co.il/ok")
        r = self.client.get("/d/" + engine.short_token(DOC))
        self.assertEqual(r.status_code, 302)   # לא הופנה ל-/login

    def test_bad_tokens_404(self):
        tok = engine.short_token(DOC)
        for bad in [tok[:-1] + ("A" if tok[-1] != "A" else "B"), "x" * 30, "short", "x" * 31]:
            self.assertEqual(self.client.get("/d/" + bad).status_code, 404, bad)

    def test_gi_failure_gives_wait_page_and_retries_once(self):
        attempts = []

        def failing(headers, doc_id):
            attempts.append(1)
            raise engine.GiError("boom")
        self.patch(engine, "gi_doc_url", failing)
        r = self.client.get("/d/" + engine.short_token(DOC))
        self.assertEqual(r.status_code, 503)
        self.assertEqual(len(attempts), 2)
        self.assertIn("loading", r.get_data(as_text=True).lower())

    def test_unknown_doc_no_retry(self):
        attempts = []
        self.patch(engine, "gi_doc_url", lambda h, d: attempts.append(1) or "")
        r = self.client.get("/d/" + engine.short_token(DOC))
        self.assertEqual(r.status_code, 503)
        self.assertEqual(len(attempts), 1)


class BalanceGuardTests(_Base):
    def _pending(self):
        app_module.PENDING["tok1"] = {"rows": list(ROWS), "bad": [], "already": [], "filename": "x.csv",
                                      "month": "2026-09", "has_gmt": False, "ts": 9e12}

    def test_preview_shows_estimate_and_balance(self):
        self.login(); self._pending()
        r = self.client.get("/preview/tok1")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("2 הודעות", html)
        self.assertIn("$10.00", html)
        self.assertNotIn("היתרה לא מספיקה", html)

    def test_full_run_blocked_when_balance_low(self):
        self.login(); self._pending()
        self.patch(engine, "twilio_balance", lambda: 0.30)   # 2 הודעות ≈ $0.52
        r = self.client.post("/run", data={"_csrf": "csrf-test", "token": "tok1"})
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("יתרת ה-SMS לא מספיקה", html)
        self.assertEqual(self.calls, [])                       # שום ריצה לא התחילה
        self.assertIn("tok1", app_module.PENDING)               # ההעלאה נשמרה לניסיון חוזר

    def test_trial_run_allowed_even_when_balance_low(self):
        self.login(); self._pending()
        self.patch(engine, "twilio_balance", lambda: 0.30)
        r = self.client.post("/run", data={"_csrf": "csrf-test", "token": "tok1", "trial": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/run/", r.headers["Location"])
        import time; time.sleep(0.2)                            # ה-worker רץ ב-thread
        self.assertEqual(self.calls, [(2, 1)])
        self.assertNotIn("tok1", app_module.PENDING)

    def test_full_run_proceeds_when_balance_ok(self):
        self.login(); self._pending()
        r = self.client.post("/run", data={"_csrf": "csrf-test", "token": "tok1"})
        self.assertEqual(r.status_code, 302)
        import time; time.sleep(0.2)
        self.assertEqual(self.calls, [(2, 0)])

    def test_balance_api_down_fails_open(self):
        self.login(); self._pending()
        self.patch(engine, "twilio_balance", lambda: None)
        r = self.client.post("/run", data={"_csrf": "csrf-test", "token": "tok1"})
        self.assertEqual(r.status_code, 302)

    def test_guard_only_for_twilio(self):
        os.environ["SEND_CHANNEL"] = "dry"
        try:
            self.assertIsNone(app_module.sms_guard(ROWS, "2026-09"))
        finally:
            os.environ["SEND_CHANNEL"] = "twilio"

    def test_csrf_still_enforced(self):
        self.login(); self._pending()
        r = self.client.post("/run", data={"token": "tok1"})
        self.assertEqual(r.status_code, 400)


class HealthTests(_Base):
    def test_health_reports_short_links(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["short_links"])
        self.assertEqual(r.get_json()["channel"], "twilio")


if __name__ == "__main__":
    unittest.main()
