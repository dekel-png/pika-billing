"""
app.py — מערכת החיובים של ג.ד. פיקה הולדינגס בע"מ — ממשק ווב למנהל החיובים.

הזרימה: התחברות → הורדת טמפלט → העלאת קובץ חיובים → תצוגה מקדימה (ולידציה +
בדיקת כפל מול חשבונית ירוקה) → אישור אנושי מפורש → הפקת חשבוניות + הודעות →
דוח ריצה. כל ריצה מדווחת לטלגרם של דקל.

אבטחה: סיסמאות ב-env (ADMIN_PASSWORD לדקל, MANAGER_PASSWORD למנהל החיובים),
CSRF על כל POST, האטת כניסות כושלות (מפתוח לפי IP אמיתי מאחורי פרוקסי Render —
הלקח מתקרית הדשבורד 16/08), סשן חתום, אין סודות בקוד.
הקשחה 21/10/2026: כותרות אבטחה על כל תגובה (HSTS, מסגור אסור, CSP בסיסי, nosniff,
Referrer-Policy, no-store), remote_addr = ה-IP האמיתי של הלקוח (behind_proxy, גם בלוגים),
ושורת לוג על כל ניסיון כניסה (login_failed / login_locked / login_blocked / login_ok) —
בלי הסיסמה שהוקלדה.
"""

from __future__ import annotations
import os
import io
import re
import csv
import hmac
import time
import uuid
import ipaddress
import threading
import urllib.request
import urllib.parse
from datetime import datetime

from flask import (Flask, render_template, request, redirect, url_for,
                   session, abort, Response, jsonify)
from werkzeug.middleware.proxy_fix import ProxyFix

import engine

try:
    from zoneinfo import ZoneInfo
    IL_TZ = ZoneInfo("Asia/Jerusalem")
except Exception:
    IL_TZ = None

_RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def valid_ip(value) -> str | None:
    """הכתובת בצורה מנורמלת, או None אם זה בכלל לא IP (כולל רשימה מופרדת בפסיקים)."""
    try:
        return str(ipaddress.ip_address((value or "").strip().strip("[]")))
    except ValueError:
        return None


def is_trusted_peer(peer: str) -> bool:
    """loopback או RFC1918 — כלומר פרוקסי שאנחנו יושבים מאחוריו (ב-Render: הפרוקסי שבתוך
    הקונטיינר, 127.0.0.1, וה-hops הפנימיים 10.x). לא CGNAT 100.64/10 ולא link-local."""
    ip = valid_ip(peer)
    if ip is None:
        return False
    addr = ipaddress.ip_address(ip)
    return addr.is_loopback or (addr.version == 4 and any(addr in n for n in _RFC1918))


def behind_proxy(wsgi_app):
    """remote_addr = ה-IP האמיתי של הלקוח, ו-https מאחורי הפרוקסי.

    נמדד ב-staging של הדשבורד 25/10: ב-Render (ש-Cloudflare הוא הקצה שלו, גם ל-*.onrender.com)
    העמית הוא 127.0.0.1/10.x ו-X-Forwarded-For = "[מה שהלקוח שלח…], לקוח, Cloudflare 172.68.x,
    Render-פנימי 10.24.x" — ה-hop האחרון משותף לכולם (מונה האטה אחד לכל העולם). לכן:
    - עמית loopback/פרטי: ה-IP מ-CF-Connecting-IP (Cloudflare קובע אותו ודוחה ב-403 עותק
      שהלקוח שולח); רק אם הוא חסר/לא תקין — ה-hop האחרון ב-XFF (ProxyFix x_for=1).
    - עמית ציבורי (גישה ישירה, בלי פרוקסי): כותרות ההעברה נמחקות ולא משפיעות.
    gunicorn לא נוגע ב-REMOTE_ADDR ולא מוחק את XFF / CF-Connecting-IP; forwarded_allow_ips
    (ברירת מחדל 127.0.0.1) משפיע רק על זיהוי ה-scheme, ו-header_map=drop זורק שמות כותרת עם "_"
    (כך ש-CF_Connecting_IP מזויף לא מתמזג לאותו מפתח). הפרוטוקול: ProxyFix x_proto=1."""
    by_last_hop = ProxyFix(wsgi_app, x_for=1, x_proto=1)
    proto_only = ProxyFix(wsgi_app, x_for=0, x_proto=1)

    def middleware(environ, start_response):
        if not is_trusted_peer(environ.get("REMOTE_ADDR") or ""):
            for k in ("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_PROTO", "HTTP_CF_CONNECTING_IP"):
                environ.pop(k, None)
            return wsgi_app(environ, start_response)
        cf = valid_ip(environ.get("HTTP_CF_CONNECTING_IP"))
        if cf:
            environ["REMOTE_ADDR"] = cf
            return proto_only(environ, start_response)
        return by_last_hop(environ, start_response)
    return middleware


app = Flask(__name__)
app.wsgi_app = behind_proxy(app.wsgi_app)
app.secret_key = os.getenv("SECRET_KEY", "")
if not app.secret_key:
    # בלי מפתח סשן אין התחברות בטוחה — נופלים למפתח אקראי (סשנים יתאפסו ברענון תהליך)
    app.secret_key = uuid.uuid4().hex
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "1") == "1",
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
)

APP_NAME = "מערכת חיובים — ג.ד. פיקה"

# מצבים בזיכרון התהליך (Render free = תהליך יחיד). איבוד ב-restart = מעלים קובץ שוב.
PENDING: dict[str, dict] = {}     # token -> {rows, bad, filename, month, ts}
RUNS: dict[str, engine.RunState] = {}
_RUNS_LOCK = threading.Lock()
_FAILED_LOGINS: dict[str, list] = {}   # throttle_key(ip) / "*" -> [timestamps]
LOGIN_MAX_FAILURES = 15                 # ניסיונות כושלים לכל IP (IPv6: לכל ‎/64)...
LOGIN_WINDOW_SEC = 900                  # ...בחלון של רבע שעה
# בלם אחרון נגד ניחוש מבוזר (הרבה כתובות): כישלונות מכל הכתובות יחד. לא חל על דפדפן שכבר
# נכנס בהצלחה (עוגיית "מכשיר מוכר" חתומה) — כך תוקף לא נועל את מנהל החיובים מחוץ למערכת.
# אותו מודל כמו בלם שם-המשתמש בדשבורד (PR #26).
LOGIN_GLOBAL_MAX = 50
_GLOBAL_KEY = "*"
_GC_AT = 1000                           # ניקוי מפתחות רדומים כשהטבלה גדלה מעבר לזה
KNOWN_COOKIE = "pb_known_device"
KNOWN_MAX_AGE = 180 * 24 * 3600
_GLOBAL_ALERT = {"ts": 0.0}             # התראת טלגרם על הבלם הכללי — לכל היותר אחת לחלון


# ------------------------------------------------------------------ עזרים

def now_il():
    return datetime.now(IL_TZ) if IL_TZ else datetime.now()


def client_ip() -> str:
    """ה-IP האמיתי של הלקוח — behind_proxy כבר הציב אותו ב-remote_addr (מ-CF-Connecting-IP
    מאחורי הפרוקסי של Render; ה-hops שהלקוח שולח ב-XFF לא נחשבים)."""
    return request.remote_addr or "?"


def throttle_key(ip: str) -> str:
    """מפתח ההאטה לכתובת: IPv4 כמו שהיא, IPv6 לפי ‎/64 (מכונה אחת מחזיקה בדרך כלל ‎/64 שלם)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _hits(key: str, now: float) -> list:
    """הכישלונות של המפתח בחלון הנוכחי; מפתח שהתרוקן נמחק מהטבלה."""
    hits = [t for t in _FAILED_LOGINS.get(key, []) if now - t < LOGIN_WINDOW_SEC]
    if hits:
        _FAILED_LOGINS[key] = hits
    else:
        _FAILED_LOGINS.pop(key, None)
    return hits


def login_throttled(ip: str, known_device: bool = False) -> str | None:
    """איזה בלם סגור: "ip" (הכתובת הזאת), "global" (כולם יחד, רק לדפדפן לא מוכר) או None."""
    now = time.time()
    if len(_hits(throttle_key(ip), now)) >= LOGIN_MAX_FAILURES:
        return "ip"
    if not known_device and len(_hits(_GLOBAL_KEY, now)) >= LOGIN_GLOBAL_MAX:
        return "global"
    return None


def record_failure(ip: str) -> tuple[int, int]:
    """רושם כישלון; מחזיר (כישלונות הכתובת, כישלונות כולם) בחלון הנוכחי."""
    now = time.time()
    if len(_FAILED_LOGINS) > _GC_AT:
        for k in list(_FAILED_LOGINS):
            _hits(k, now)
    key = throttle_key(ip)
    _FAILED_LOGINS.setdefault(key, []).append(now)
    _FAILED_LOGINS.setdefault(_GLOBAL_KEY, []).append(now)
    return len(_FAILED_LOGINS[key]), len(_FAILED_LOGINS[_GLOBAL_KEY])


def _known_signer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(app.secret_key, salt="login-known-device")


def is_known_device() -> bool:
    """הדפדפן הזה כבר נכנס בהצלחה (עוגייה חתומה במפתח הסשן; זיוף/פג תוקף = לא מוכר)."""
    raw = request.cookies.get(KNOWN_COOKIE)
    if not raw:
        return False
    try:
        return _known_signer().loads(raw, max_age=KNOWN_MAX_AGE) == 1
    except Exception:
        return False


def security_log(event: str, ip: str, **fields) -> None:
    """שורת לוג אחת ל-stdout (נאסף ע"י Render). לעולם לא מעבירים לכאן סיסמה או סוד."""
    safe_ip = re.sub(r"[^0-9A-Fa-f:.]", "", ip)[:64] or "?"
    extra = "".join(f" {k}={re.sub(r'[^0-9A-Za-z_.:-]', '', str(v))[:64]}"
                    for k, v in fields.items())
    print(f"{now_il().isoformat(timespec='seconds')} {event} ip={safe_ip}{extra}", flush=True)


def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = uuid.uuid4().hex
    return session["_csrf"]


def check_csrf():
    tok = request.form.get("_csrf", "")
    if not tok or not hmac.compare_digest(tok, session.get("_csrf", "")):
        abort(400, "CSRF")


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["APP_NAME"] = APP_NAME


# כותרות אבטחה על כל תגובה (כולל 404/500). כל הטפסים שולחים לאותו מקור ⇒ form-action 'self';
# אין script-src בכוונה (run.html מריץ JS משובץ). ראוט שכבר קבע כותרת — נשאר בערך שלו.
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": ("frame-ancestors 'none'; object-src 'none'; "
                                "base-uri 'self'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
}
_TOKEN_URL_ARGS = ("token", "run_id")   # כתובות שנושאות טוקן/מזהה ריצה — בלי Referer בכלל


@app.after_request
def security_headers(resp):
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    token_url = any(k in (request.view_args or {}) for k in _TOKEN_URL_ARGS)
    resp.headers.setdefault("Referrer-Policy", "no-referrer" if token_url else "same-origin")
    # אין כאן תוכן ציבורי שכדאי לשמור במטמון: דפים מחוברים, לינקי טוקן, CSV וטופס עם CSRF
    if request.endpoint != "static":
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def logged_in() -> bool:
    return bool(session.get("role"))


def require_login():
    if not logged_in():
        return redirect(url_for("login"))
    return None


def notify_telegram(text: str):
    """דיווח לדקל על כל ריצה. נכשל בשקט — לא מפיל את המערכת."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                   data=data), timeout=10)
    except Exception:
        pass


# ------------------------------------------------------------------ ראוטים

@app.get("/health")
def health():
    return {"ok": True, "app": "pika-billing", "channel": engine.channel(),
            "short_links": engine.short_links_enabled(),
            "time": now_il().isoformat(timespec="seconds")}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        check_csrf()
        ip = client_ip()
        known = is_known_device()
        brake = login_throttled(ip, known)
        if brake:
            security_log("login_blocked", ip, scope=brake, known=int(known),
                         attempts=len(_FAILED_LOGINS.get(
                             throttle_key(ip) if brake == "ip" else _GLOBAL_KEY, [])))
            error = "יותר מדי ניסיונות. לנסות שוב בעוד רבע שעה."
        else:
            pw = request.form.get("password", "")
            admin = os.getenv("ADMIN_PASSWORD", "")
            manager = os.getenv("MANAGER_PASSWORD", "")
            role = None
            if admin and hmac.compare_digest(pw, admin):
                role = "admin"
            elif manager and hmac.compare_digest(pw, manager):
                role = "manager"
            if role:
                session.clear()
                session["role"] = role
                session["_csrf"] = uuid.uuid4().hex
                security_log("login_ok", ip, role=role, known=int(known))
                resp = redirect(url_for("dashboard"))
                resp.set_cookie(KNOWN_COOKIE, _known_signer().dumps(1), max_age=KNOWN_MAX_AGE,
                                httponly=True, samesite="Lax",
                                secure=app.config["SESSION_COOKIE_SECURE"])
                return resp
            attempts, total = record_failure(ip)
            security_log("login_failed", ip, attempts=attempts, total=total, known=int(known))
            if attempts == LOGIN_MAX_FAILURES:
                security_log("login_locked", ip, scope="ip", attempts=attempts,
                             minutes=LOGIN_WINDOW_SEC // 60)
            if total == LOGIN_GLOBAL_MAX:
                security_log("login_locked", ip, scope="global", attempts=total,
                             minutes=LOGIN_WINDOW_SEC // 60)
                if time.time() - _GLOBAL_ALERT["ts"] >= LOGIN_WINDOW_SEC:
                    _GLOBAL_ALERT["ts"] = time.time()
                    threading.Thread(target=notify_telegram, daemon=True, args=(
                        f"⚠ מערכת החיובים: {total} כניסות כושלות ברבע שעה מכל הכתובות — "
                        "הכניסה נחסמה לדפדפנים לא מוכרים לרבע שעה (דפדפן שכבר נכנס — עובד כרגיל).",)).start()
            time.sleep(1)
            error = "סיסמה שגויה"
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def dashboard():
    if (r := require_login()):
        return r
    month = now_il().strftime("%Y-%m")
    gi_ok, gi_msg, billed_count = False, "", 0
    try:
        headers = engine.gi_token()
        engine.gi_verify_business(headers)
        gi_ok = True
        billed_count = len(engine.gi_billed_phones(headers, month))
        gi_msg = "מחובר ומאומת: " + engine.BUSINESS_NAME
    except Exception as e:
        gi_msg = str(e)[:200]
    runs = sorted(RUNS.values(), key=lambda s: s.started, reverse=True)[:10]
    sms_balance = engine.twilio_balance()   # None אם לא twilio / ה-API לא ענה
    return render_template("dashboard.html", month=month, gi_ok=gi_ok, gi_msg=gi_msg,
                           billed_count=billed_count, channel=engine.channel(),
                           runs=runs, role=session.get("role"),
                           sms_balance=sms_balance,
                           segment_cost=engine.SMS_SEGMENT_COST_USD)


TEMPLATE_HEADERS = ["מספר דרכון", "שם מלא", "מספר טלפון", "סכום", "מספר חשבון GMT"]
TEMPLATE_ROWS = [
    ["N1234567", "Somchai Prasert", "0501234567", "55", "100200300"],
    ["EB0891011", "Ivan Petrov", "052-2345678", "60.5", "100200301"],
]


@app.get("/template.csv")
def template_csv():
    if (r := require_login()):
        return r
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TEMPLATE_HEADERS)
    for row in TEMPLATE_ROWS:
        w.writerow(row)
    data = "﻿" + buf.getvalue()
    return Response(data, mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=charges-template.csv"})


@app.get("/template.xlsx")
def template_xlsx():
    """טמפלט אקסל שבו עמודות הטלפון/דרכון/GMT כבר מוגדרות כטקסט —
    האפס המוביל שורד ואין 5.01E+08."""
    if (r := require_login()):
        return r
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "חיובים"
    ws.sheet_view.rightToLeft = True
    ws.append(TEMPLATE_HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="12263A")
    text_cols = (1, 3, 5)   # דרכון, טלפון, GMT — טקסט מפורש
    for row in TEMPLATE_ROWS:
        ws.append(row)
    for col_idx in text_cols:
        for row_idx in range(1, 201):    # גם שורות עתידיות שימולאו בקובץ
            ws.cell(row=row_idx, column=col_idx).number_format = "@"
    widths = [16, 24, 16, 10, 20]
    for i, wdt in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + i)].width = wdt
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return Response(out.read(),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             "attachment; filename=charges-template.xlsx"})


@app.post("/upload")
def upload():
    if (r := require_login()):
        return r
    check_csrf()
    f = request.files.get("charges")
    if not f or not f.filename:
        return render_template("dashboard_error.html", error="לא נבחר קובץ")
    month = request.form.get("month", "").strip() or now_il().strftime("%Y-%m")
    try:
        good, bad = engine.load_charges(f.read(), f.filename)
    except ValueError as e:
        return render_template("dashboard_error.html", error=str(e))
    messages_only = request.form.get("messages_only") == "1"
    already = []
    try:
        headers = engine.gi_token()
        engine.gi_verify_business(headers)
        billed = engine.gi_billed_phones(headers, month)
        if messages_only:
            # הודעות בלבד: מי שכבר יש לו מסמך הוא היעד; מי שאין לו — המנוע ידלג עליו
            good = [r_ for r_ in good if r_["phone"] in billed] + \
                   [r_ for r_ in good if r_["phone"] not in billed]
        else:
            already = [r_ for r_ in good if r_["phone"] in billed]
            good = [r_ for r_ in good if r_["phone"] not in billed]
    except Exception as e:
        return render_template("dashboard_error.html",
                               error=f"חשבונית ירוקה לא זמינה כרגע: {str(e)[:200]}")
    token = uuid.uuid4().hex
    PENDING[token] = {"rows": good, "bad": bad, "already": already,
                      "filename": f.filename, "month": month,
                      "messages_only": messages_only,
                      "has_gmt": any(r_.get("gmt") for r_ in good),
                      "ts": time.time()}
    # ניקוי העלאות ישנות מהזיכרון
    for k in [k for k, v in PENDING.items() if time.time() - v["ts"] > 3600]:
        PENDING.pop(k, None)
    return redirect(url_for("preview", token=token))


@app.get("/preview/<token>")
def preview(token):
    if (r := require_login()):
        return r
    p = PENDING.get(token)
    if not p:
        return render_template("dashboard_error.html",
                               error="ההעלאה פגה — להעלות את הקובץ שוב")
    total = sum(r_["amount"] for r_ in p["rows"])
    return render_template("preview.html", p=p, token=token, total=total,
                           channel=engine.channel(),
                           sms=sms_guard(p["rows"], p["month"]))


@app.post("/run")
def start_run():
    if (r := require_login()):
        return r
    check_csrf()
    token = request.form.get("token", "")
    p = PENDING.get(token)
    if not p:
        return render_template("dashboard_error.html",
                               error="ההעלאה פגה — להעלות את הקובץ שוב")
    if not p["rows"]:
        return render_template("dashboard_error.html", error="אין שורות תקינות להפקה")
    limit = 1 if request.form.get("trial") == "1" else 0
    messages_only = bool(p.get("messages_only"))
    if not limit:
        # הכול-או-כלום (הוראת דקל 16/09): הריצה לא מתחילה עד שהיתרה מכסה את כל ההודעות
        # במחיר המקסימלי של Twilio לישראל (נשלף חי). ההעלאה נשארת בזיכרון — מטעינים,
        # חוזרים לתצוגה המקדימה ומאשרים. חצי גל = עובדים עם מסמך בספרים ובלי הודעה — לא כאן.
        g = sms_guard(p["rows"], p["month"])
        if g and g["blocked"]:
            return render_template(
                "dashboard_error.html",
                error=(f"יתרת ה-SMS לא מספיקה לכל הגל, והמערכת שולחת הכול או כלום: "
                       f"נדרש ${g['usd']:.2f} ({g['messages']} הודעות × ${g['price']:.4f}), "
                       f"בחשבון Twilio יש ${g['balance']:.2f} — חסרים ${g['shortfall']:.2f}. "
                       "להטעין את Twilio ואז לחזור לתצוגה המקדימה ולאשר שוב. "
                       "ריצת ניסיון של שורה אחת אפשרית גם עכשיו."))
    PENDING.pop(token, None)
    run_id = uuid.uuid4().hex[:12]
    state = engine.RunState(run_id=run_id, month=p["month"], messages_only=messages_only,
                            started=now_il().isoformat(timespec="seconds"))
    with _RUNS_LOCK:
        RUNS[run_id] = state

    rows = p["rows"]

    def worker():
        engine.execute_run(state, rows, p["month"], limit=limit, messages_only=messages_only)
        mode = " (ריצת ניסיון — שורה אחת)" if limit else ""
        if state.status != "finished":
            notify_telegram(f"❌ ריצת חיובי סים {p['month']} נעצרה: {state.error}")
        elif messages_only:
            notify_telegram(
                f"📨 שליחה חוזרת של הודעות — חיובי סים {p['month']}{mode}\n"
                f"נשלחו {state.sent_count} · לא נשלחו {len(state.unsent)} · "
                f"בלי מסמך (דולגו) {len(state.skipped)}\nקובץ: {p['filename']}")
        else:
            unsent = len(state.unsent)
            notify_telegram(
                f"📱 חיובי סים {p['month']}{mode}\n"
                f"הופקו {state.ok_count} חשבוניות על סך {state.ok_total:,.2f} ₪ · "
                f"נכשלו {state.fail_count} · דולגו {len(state.skipped)}"
                + (f"\n⚠ {unsent} הודעות לא נשלחו — CSV לשליחה חוזרת בדוח הריצה" if unsent else "")
                + f"\nקובץ: {p['filename']}")

    threading.Thread(target=worker, daemon=True).start()
    return redirect(url_for("run_page", run_id=run_id))


@app.get("/run/<run_id>")
def run_page(run_id):
    if (r := require_login()):
        return r
    state = RUNS.get(run_id)
    if not state:
        return render_template("dashboard_error.html", error="ריצה לא נמצאה")
    return render_template("run.html", state=state)


@app.get("/api/run/<run_id>")
def run_status(run_id):
    if (r := require_login()):
        return r
    state = RUNS.get(run_id)
    if not state:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": state.status, "done": state.done, "total": state.total,
                    "ok": state.ok_count, "fail": state.fail_count,
                    "error": state.error})


@app.route("/test-message", methods=["GET", "POST"])
def test_message():
    """אדמין בלבד: שליחת הודעת בדיקה אחת למספר שמוקלד — אימות ערוץ בלי חיובים."""
    if (r := require_login()):
        return r
    if session.get("role") != "admin":
        abort(403)
    result, error = "", ""
    if request.method == "POST":
        check_csrf()
        phone, perr = engine.normalize_phone(request.form.get("phone", ""))
        if perr:
            error = perr
        else:
            try:
                how = engine.send_message(
                    phone,
                    "הודעת בדיקה ממערכת החיובים של ג.ד. פיקה — הערוץ עובד ✓\n"
                    "Test message from G.D. Pika billing system — channel OK ✓")
                result = f"נשלח בהצלחה ({how}) אל 0{phone[3:]}"
            except Exception as e:
                error = f"השליחה נכשלה: {str(e)[:250]}"
    return render_template("test_message.html", result=result, error=error,
                           channel=engine.channel())


# ------------------------------------------------------- לינק קצר מה-SMS
_GI_CACHE: dict = {"headers": None, "ts": 0.0}
_GI_CACHE_LOCK = threading.Lock()


def gi_headers_cached(force: bool = False) -> dict:
    """טוקן חשבונית ירוקה לשימוש חוזר (25 דק') — לא מתחברים מחדש על כל לחיצה על לינק."""
    with _GI_CACHE_LOCK:
        if not force and _GI_CACHE["headers"] and time.time() - _GI_CACHE["ts"] < 1500:
            return _GI_CACHE["headers"]
        h = engine.gi_token()
        _GI_CACHE.update(headers=h, ts=time.time())
        return h


def sms_guard(rows: list, month: str):
    """אומדן עלות ההודעות מול יתרת Twilio. None כשלא רלוונטי (ערוץ אחר / אין שורות)."""
    if engine.channel() != "twilio" or not rows:
        return None
    est = engine.sms_wave_estimate(rows, month)
    bal = engine.twilio_balance()
    est["balance"] = bal
    est["blocked"] = bal is not None and bal < est["usd"]
    est["shortfall"] = round(max(0.0, est["usd"] - bal), 2) if bal is not None else None
    est["short_links"] = engine.short_links_enabled()
    return est


@app.get("/d/<token>")
def short_doc(token):
    """הלינק הקצר מה-SMS → הפניה ללינק החתום הטרי של המסמך בחשבונית ירוקה.
    ציבורי (בלי לוגין): ה-token חתום ב-HMAC על SECRET_KEY ואינו ניתן לניחוש;
    המסמך עצמו מוגש ע"י חשבונית ירוקה, לא מכאן."""
    doc_id = engine.resolve_short_token(token)
    if not doc_id:
        abort(404)
    url = ""
    for attempt in (0, 1):
        try:
            url = engine.gi_doc_url(gi_headers_cached(force=bool(attempt)), doc_id)
            break
        except Exception:
            url = ""          # טוקן פג / תקלה זמנית — ניסיון אחד נוסף עם טוקן טרי
    if not url:
        return render_template("link_wait.html"), 503
    resp = redirect(url, code=302)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/report/<run_id>")
def report(run_id):
    if (r := require_login()):
        return r
    state = RUNS.get(run_id)
    if not state:
        return render_template("dashboard_error.html",
                               error="הדוח לא בזיכרון (אחרי עדכון גרסה) — הנתונים המלאים בחשבונית ירוקה")
    return render_template("report.html", state=state)


@app.get("/report/<run_id>/unsent.csv")
def report_unsent_csv(run_id):
    """השורות שיש להן מסמך אבל ההודעה לא נשלחה — בפורמט הטמפלט, להעלאה חוזרת
    במצב "הודעות בלבד" אחרי הטענת SMS."""
    if (r := require_login()):
        return r
    state = RUNS.get(run_id)
    if not state:
        return render_template("dashboard_error.html",
                               error="הדוח לא בזיכרון (אחרי עדכון גרסה) — הנתונים המלאים בחשבונית ירוקה")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TEMPLATE_HEADERS)
    for it in state.unsent:
        w.writerow([it["passport"], it["name"], it["phone"], f"{it['amount']:g}", it.get("gmt", "")])
    data = "\ufeff" + buf.getvalue()
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="unsent-{state.month}-{run_id}.csv"'})


# ------------------------------------------------------------- keep-alive
# Render free מרדים את השירות אחרי 15 דק' בלי תעבורה — והעובד שלוחץ על הלינק
# מההודעה מחכה ~30 שניות. פינג עצמי כל 10 דק' (רק בענן; KEEP_ALIVE=0 מכבה).
def _keep_alive_loop():
    url = os.getenv("KEEP_ALIVE_URL", f"{engine.PUBLIC_BASE_URL}/health")
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(url, timeout=20).read()
        except Exception:
            pass


if os.getenv("KEEP_ALIVE", "1") == "1" and os.getenv("RENDER"):
    threading.Thread(target=_keep_alive_loop, daemon=True, name="keep-alive").start()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5480, debug=False)
