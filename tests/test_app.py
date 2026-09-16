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
os.environ.pop("TWILIO_ACCOUNT_SID", None)   # בלי מחירון חי בבדיקות — פולבק דטרמיניסטי
os.environ.pop("TWILIO_AUTH_TOKEN", None)
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
        self.patch(engine, "execute_run",
                   lambda state, rows, month, limit=0, messages_only=False: self.calls.append((len(rows), limit, messages_only)))
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
        self.assertIn("חסרים $", html)
        self.assertEqual(self.calls, [])                       # שום ריצה לא התחילה
        self.assertIn("tok1", app_module.PENDING)               # ההעלאה נשמרה לניסיון חוזר

    def test_trial_run_allowed_even_when_balance_low(self):
        self.login(); self._pending()
        self.patch(engine, "twilio_balance", lambda: 0.30)
        r = self.client.post("/run", data={"_csrf": "csrf-test", "token": "tok1", "trial": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/run/", r.headers["Location"])
        import time; time.sleep(0.2)                            # ה-worker רץ ב-thread
        self.assertEqual(self.calls, [(2, 1, False)])
        self.assertNotIn("tok1", app_module.PENDING)

    def test_full_run_proceeds_when_balance_ok(self):
        self.login(); self._pending()
        r = self.client.post("/run", data={"_csrf": "csrf-test", "token": "tok1"})
        self.assertEqual(r.status_code, 302)
        import time; time.sleep(0.2)
        self.assertEqual(self.calls, [(2, 0, False)])

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


class MessagesOnlyTests(_Base):
    CSV = ("\ufeffמספר דרכון,שם מלא,מספר טלפון,סכום,מספר חשבון GMT\n"
           "N1,Somchai Prasert,0501234567,55,\n"
           "N2,Ivan Petrov,0522345678,60.5,100200301\n").encode("utf-8")

    def _upload(self, messages_only):
        import io as _io
        self.patch(engine, "gi_token", lambda: {})
        self.patch(engine, "gi_verify_business", lambda h: "ok")
        self.patch(engine, "gi_billed_phones", lambda h, m: {"972501234567"})   # לסומצ'אי כבר יש מסמך
        data = {"_csrf": "csrf-test", "month": "2026-09", "charges": (_io.BytesIO(self.CSV), "x.csv")}
        if messages_only:
            data["messages_only"] = "1"
        r = self.client.post("/upload", data=data, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        tok = r.headers["Location"].rsplit("/", 1)[-1]
        return app_module.PENDING[tok], tok

    def test_normal_upload_splits_already_billed(self):
        self.login()
        p, _ = self._upload(messages_only=False)
        self.assertFalse(p["messages_only"])
        self.assertEqual([r["name"] for r in p["rows"]], ["Ivan Petrov"])
        self.assertEqual([r["name"] for r in p["already"]], ["Somchai Prasert"])

    def test_messages_only_upload_keeps_everyone(self):
        self.login()
        p, tok = self._upload(messages_only=True)
        self.assertTrue(p["messages_only"])
        self.assertEqual(len(p["rows"]), 2)
        self.assertEqual(p["already"], [])
        html = self.client.get(f"/preview/{tok}").get_data(as_text=True)
        self.assertIn("הודעות בלבד", html)
        self.assertIn("שליחת הודעות", html)
        r = self.client.post("/run", data={"_csrf": "csrf-test", "token": tok})
        self.assertEqual(r.status_code, 302)
        import time; time.sleep(0.2)
        self.assertEqual(self.calls, [(2, 0, True)])

    def test_all_or_nothing_no_force_override(self):
        """הוראת דקל 16/09: לא חצי-חצי — אין דרך להריץ גל שהיתרה לא מכסה במלואו."""
        self.login()
        app_module.PENDING["tok1"] = {"rows": list(ROWS), "bad": [], "already": [], "filename": "x.csv",
                                      "month": "2026-09", "has_gmt": False, "ts": 9e12}
        self.patch(engine, "twilio_balance", lambda: 0.30)
        html = self.client.get("/preview/tok1").get_data(as_text=True)
        self.assertNotIn('name="force"', html)
        self.assertIn("הכול או כלום", html)
        self.assertIn("חסרים $", html)
        r = self.client.post("/run", data={"_csrf": "csrf-test", "token": "tok1", "force": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("הכול או כלום", r.get_data(as_text=True))
        self.assertEqual(self.calls, [])
        self.assertIn("tok1", app_module.PENDING)

    def test_guard_reports_exact_shortfall(self):
        self.patch(engine, "twilio_balance", lambda: 0.30)
        g = app_module.sms_guard(ROWS, "2026-09")
        self.assertTrue(g["blocked"])
        self.assertAlmostEqual(g["shortfall"], round(2 * engine.SMS_SEGMENT_COST_USD - 0.30, 2))
        self.patch(engine, "twilio_balance", lambda: 10.0)
        g = app_module.sms_guard(ROWS, "2026-09")
        self.assertFalse(g["blocked"])
        self.assertEqual(g["shortfall"], 0.0)

    def test_unsent_csv_lists_only_unsent(self):
        self.login()
        st = engine.RunState(run_id="r1", month="2026-09", status="finished", started="2026-09-16T10:00:00")
        st.results = [
            {"passport": "N1", "name": "A", "phone": "0501111111", "amount": 55.0, "gmt": "", "ok": True, "sent": True,
             "doc_number": "1", "doc_url": "", "delivery": "SMS", "error": ""},
            {"passport": "N2", "name": "B", "phone": "0502222222", "amount": 60.5, "gmt": "100", "ok": True, "sent": False,
             "doc_number": "2", "doc_url": "", "delivery": engine.FAILED_PREFIX + ": boom", "error": ""},
            {"passport": "N3", "name": "C", "phone": "0503333333", "amount": 10.0, "gmt": "", "ok": False, "sent": False,
             "doc_number": "", "doc_url": "", "delivery": "", "error": "GI down"},
        ]
        app_module.RUNS["r1"] = st
        html = self.client.get("/report/r1").get_data(as_text=True)
        self.assertIn("לא נשלחו", html)
        self.assertIn("unsent.csv", html)
        r = self.client.get("/report/r1/unsent.csv")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertTrue(body.startswith("\ufeff"))
        lines = [l for l in body.lstrip("\ufeff").splitlines() if l.strip()]
        self.assertEqual(lines[0], "מספר דרכון,שם מלא,מספר טלפון,סכום,מספר חשבון GMT")
        self.assertEqual(lines[1:], ["N2,B,0502222222,60.5,100"])


class HealthTests(_Base):
    def test_health_reports_short_links(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["short_links"])
        self.assertEqual(r.get_json()["channel"], "twilio")


if __name__ == "__main__":
    unittest.main()
