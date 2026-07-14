# ═══════════════════════════════════════════════════════════════════════════
#  server.py — STE AL RIADH · Tableau de bord cloud (read-only)
#
#  FastAPI backend over the Railway PostgreSQL schema fed by AbdouProSync.
#  One aggregate endpoint (/api/dashboard) powers the whole dashboard so the
#  client polls a single round-trip; /healthz for Railway health checks.
#
#  Local run :  uvicorn server:app --reload
#  Production:  Procfile → uvicorn server:app --host 0.0.0.0 --port $PORT
# ═══════════════════════════════════════════════════════════════════════════

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime

import psycopg2
import psycopg2.extras
import psycopg2.pool
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# No credentials in code — the DATABASE_URL environment variable is required.
# On Railway: set DATABASE_URL = ${{Postgres.DATABASE_URL}} in the service
# variables. Locally: set it in your shell before running uvicorn.
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set — refusing to start."
    )

# ── Users: credentials come from env vars, never from code ──────────────────
#   DASH_SUPERADMIN = username:password     (full dashboard)
#   DASH_ADMIN      = username:password     (operations view — no credit
#                                            details, no withdrawals)
def _load_users():
    users = {}
    for var, role in (("DASH_SUPERADMIN", "superadmin"), ("DASH_ADMIN", "admin")):
        raw = os.environ.get(var, "")
        if raw and ":" in raw:
            u, p = raw.split(":", 1)
            users[u.strip()] = {"password": p, "role": role}
    return users

USERS = _load_users()

# Session-signing key: SECRET_KEY env var if provided, otherwise derived from
# DATABASE_URL (already secret) so no extra mandatory variable is needed.
SECRET = (os.environ.get("SECRET_KEY")
          or hashlib.sha256(("ar-dash:" + DATABASE_URL).encode()).hexdigest()
          ).encode()

SESSION_COOKIE = "ar_session"
SESSION_DAYS = 7

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="STE AL RIADH — Monitoring", docs_url=None, redoc_url=None)

_pool = None

def pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(
            1, 4, DATABASE_URL, connect_timeout=10)
    return _pool


@contextmanager
def db():
    p = pool()
    conn = p.getconn()
    try:
        yield conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        conn.commit()
    except psycopg2.Error:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        p.putconn(conn)


def rows(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()


def one(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchone()


# ── Sessions (signed cookie, HMAC-SHA256) ────────────────────────────────────

def make_token(username, role):
    payload = json.dumps({"u": username, "r": role,
                          "exp": int(time.time()) + SESSION_DAYS * 86400})
    b = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(SECRET, b.encode(), hashlib.sha256).hexdigest()
    return f"{b}.{sig}"


def parse_token(tok):
    try:
        b, sig = tok.split(".")
        good = hmac.new(SECRET, b.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        payload = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def current_user(request: Request):
    tok = request.cookies.get(SESSION_COOKIE)
    p = parse_token(tok) if tok else None
    # A user removed from the env vars is revoked even with a valid cookie
    if not p or p.get("u") not in USERS:
        raise HTTPException(401, "non authentifié")
    return {"username": p["u"], "role": USERS[p["u"]]["role"]}


# Tiny brute-force brake: after 8 failures per IP, 60s lockout.
_fail_lock = threading.Lock()
_failures = {}   # ip -> [count, locked_until]

def _brute_check(ip):
    with _fail_lock:
        cnt, until = _failures.get(ip, (0, 0))
        if time.time() < until:
            raise HTTPException(429, "trop de tentatives — réessayez dans 1 min")

def _brute_note(ip, ok):
    with _fail_lock:
        if ok:
            _failures.pop(ip, None)
        else:
            cnt, _ = _failures.get(ip, (0, 0))
            cnt += 1
            _failures[ip] = (cnt, time.time() + 60 if cnt >= 8 else 0)


# ── Web-side tables (owned by the dashboard, not touched by the sync) ────────

_web_ready = False

def ensure_web_tables(cur):
    global _web_ready
    if _web_ready:
        return
    cur.execute("""
        CREATE TABLE IF NOT EXISTS web_notifications (
            id         bigserial PRIMARY KEY,
            created_at timestamptz NOT NULL DEFAULT now(),
            category   text,
            severity   text,
            title      text,
            body       text,
            dedupe_key text
        );
        CREATE INDEX IF NOT EXISTS idx_webnotif_created
            ON web_notifications (created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_webnotif_dedupe
            ON web_notifications (dedupe_key, created_at DESC);
        CREATE TABLE IF NOT EXISTS web_user_state (
            username     text PRIMARY KEY,
            last_seen_at timestamptz NOT NULL DEFAULT 'epoch'
        );
    """)
    _web_ready = True


# ── Alert engine — derives notifications from the data ──────────────────────
#    Runs in a background thread (one pass per minute), never inside a web
#    request: a pass is dozens of queries and must not add request latency.

_engine_lock = threading.Lock()
_engine_last = 0.0

def _emit(cur, key, cooldown_h, category, severity, title, body):
    """Insert an alert unless the same dedupe_key fired within cooldown."""
    cur.execute(
        "SELECT 1 FROM web_notifications WHERE dedupe_key = %s "
        "AND created_at > now() - %s * interval '1 hour' LIMIT 1",
        (key, cooldown_h))
    if cur.fetchone():
        return
    cur.execute(
        "INSERT INTO web_notifications (category, severity, title, body, "
        "dedupe_key) VALUES (%s, %s, %s, %s, %s)",
        (category, severity, title, body, key))


def run_alert_engine(cur, threshold):
    if not _engine_lock.acquire(blocking=False):
        return
    try:
        # 1) Stock: rupture + stock bas
        for r in rows(cur, """
                SELECT p.id, p.name,
                       coalesce(s.carton_qty,0)  AS shop_c,
                       coalesce(dp.carton_qty,0) AS depot_c
                FROM products p
                LEFT JOIN stock_shop  s  ON s.product_id  = p.id
                LEFT JOIN stock_depot dp ON dp.product_id = p.id"""):
            total = r["shop_c"] + r["depot_c"]
            if total == 0:
                _emit(cur, f"rupture:{r['id']}", 12, "stock", "danger",
                      "Rupture de stock",
                      f"{r['name']} : 0 carton en boutique et au dépôt")
            elif r["shop_c"] < threshold:
                _emit(cur, f"bas:{r['id']}", 12, "stock", "warning",
                      "Stock boutique bas",
                      f"{r['name']} : {r['shop_c']} carton(s) en boutique "
                      f"({r['depot_c']} au dépôt)")

        # 2) Credit exposure per customer
        for r in rows(cur, """
                SELECT c.customer_id, c.name, sum(t.amount) AS unpaid
                FROM credit_transactions t
                JOIN credit_customers c ON c.customer_id = t.customer_id
                WHERE t.paid_status = 'UNPAID'
                GROUP BY c.customer_id, c.name
                HAVING sum(t.amount) >= 1500"""):
            _emit(cur, f"credit:{r['customer_id']}", 24, "credit", "warning",
                  "Encours crédit élevé",
                  f"{r['name']} : {float(r['unpaid']):.2f} DT d'impayés")

        # 3) Big sales today (once per sale)
        for r in rows(cur, """
                SELECT sale_id, total, sale_source FROM sales
                WHERE datetime >= current_date AND total >= 1000"""):
            _emit(cur, f"bigsale:{r['sale_id']}", 24 * 365, "sales", "success",
                  "Grosse vente",
                  f"{float(r['total']):.2f} DT — "
                  f"{'Dépôt' if r['sale_source'] == 'DEPOT' else 'Boutique'}")

        # 4) Sync offline > 15 min (re-alert every 2h while it lasts)
        last = one(cur, "SELECT max(synced_at) AS t FROM sync_meta")
        if last and last["t"]:
            cur.execute("SELECT extract(epoch FROM now() - %s) AS age",
                        (last["t"],))
            if cur.fetchone()["age"] > 900:
                _emit(cur, "sync_offline", 2, "system", "danger",
                      "Synchronisation interrompue",
                      "Aucune donnée reçue du magasin depuis plus de 15 minutes")
    finally:
        _engine_lock.release()


def _engine_loop():
    while True:
        try:
            # DDL in its own short transaction: holding schema locks open
            # for the whole pass would block concurrent web requests.
            with db() as cur:
                ensure_web_tables(cur)
            with db() as cur:
                s = one(cur, "SELECT data FROM app_settings WHERE id = 1")
                thr = int((s["data"].get("low_stock_threshold", 5)
                           if s else 5) or 5)
                run_alert_engine(cur, thr)
        except Exception as e:
            print(f"[alert-engine] {type(e).__name__}: "
                  f"{str(e).strip().splitlines()[0] if str(e).strip() else ''}")
        time.sleep(60)


@app.on_event("startup")
def _start_engine():
    threading.Thread(target=_engine_loop, daemon=True).start()


# ── API ──────────────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    try:
        with db() as cur:
            one(cur, "SELECT 1 AS ok")
        return {"ok": True}
    except Exception:
        return JSONResponse({"ok": False}, status_code=503)


@app.post("/api/login")
async def login(request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    _brute_check(ip)
    if not USERS:
        raise HTTPException(503, "authentification non configurée — définir "
                                 "DASH_SUPERADMIN et DASH_ADMIN")
    body = await request.json()
    u = (body.get("username") or "").strip()
    pw = body.get("password") or ""
    rec = USERS.get(u)
    ok = bool(rec) and hmac.compare_digest(rec["password"], pw)
    _brute_note(ip, ok)
    if not ok:
        raise HTTPException(401, "identifiants invalides")
    secure = request.headers.get("x-forwarded-proto",
                                 request.url.scheme) == "https"
    response.set_cookie(SESSION_COOKIE, make_token(u, rec["role"]),
                        max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=secure)
    return {"username": u, "role": rec["role"]}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


@app.post("/api/notifications/seen")
def notifications_seen(user=Depends(current_user)):
    try:
        with db() as cur:
            ensure_web_tables(cur)
            cur.execute(
                "INSERT INTO web_user_state (username, last_seen_at) "
                "VALUES (%s, now()) ON CONFLICT (username) "
                "DO UPDATE SET last_seen_at = now()", (user["username"],))
        return {"ok": True}
    except psycopg2.Error:
        raise HTTPException(503, "database unavailable")


@app.get("/api/dashboard")
def dashboard(user=Depends(current_user)):
    try:
        with db() as cur:
            return build_dashboard(cur, user)
    except psycopg2.Error as e:
        print(f"[dashboard] {type(e).__name__}: "
              f"{str(e).strip().splitlines()[0] if str(e).strip() else ''}")
        raise HTTPException(503, "database unavailable")


def build_dashboard(cur, user):
    d = {"user": user}

    settings = one(cur, "SELECT data FROM app_settings WHERE id = 1")
    d["settings"] = settings["data"] if settings else {}
    threshold = int(d["settings"].get("low_stock_threshold", 5) or 5)

    ensure_web_tables(cur)

    d["freshness"] = rows(cur, """
        SELECT file_name, synced_at, row_count FROM sync_meta
        ORDER BY synced_at DESC""")
    d["last_sync"] = str(d["freshness"][0]["synced_at"]) if d["freshness"] else None
    d["server_now"] = datetime.utcnow().isoformat() + "Z"

    # KPIs — today vs yesterday
    d["today"] = one(cur, """
        SELECT coalesce(sum(total),0) AS revenue,
               count(*)               AS tickets,
               coalesce(sum(total) FILTER (WHERE sale_source='BOUTIQUE'),0) AS boutique,
               coalesce(sum(total) FILTER (WHERE sale_source='DEPOT'),0)    AS depot,
               coalesce(sum(total) FILTER (WHERE payment_type='Credit'),0)  AS credit_amount,
               coalesce(avg(total),0) AS avg_ticket
        FROM sales WHERE datetime::date = current_date""")
    d["yesterday"] = one(cur, """
        SELECT coalesce(sum(total),0) AS revenue, count(*) AS tickets
        FROM sales WHERE datetime::date = current_date - 1""")

    d["credit"] = one(cur, """
        SELECT coalesce(sum(amount),0) AS unpaid_total,
               count(*) FILTER (WHERE paid_status='UNPAID') AS unpaid_count
        FROM credit_transactions WHERE paid_status = 'UNPAID'""")

    d["withdrawals_today"] = one(cur, """
        SELECT coalesce(sum(amount),0) AS total FROM withdrawals
        WHERE datetime::date = current_date""")

    # 14-day revenue, split boutique / dépôt
    d["revenue_14d"] = rows(cur, """
        SELECT to_char(day, 'YYYY-MM-DD') AS day,
               coalesce(b.rev, 0) AS boutique, coalesce(dp.rev, 0) AS depot
        FROM generate_series(current_date - 13, current_date, '1 day') AS g(day)
        LEFT JOIN (SELECT datetime::date d, sum(total) rev FROM sales
                   WHERE sale_source='BOUTIQUE' GROUP BY 1) b ON b.d = g.day
        LEFT JOIN (SELECT datetime::date d, sum(total) rev FROM sales
                   WHERE sale_source='DEPOT' GROUP BY 1) dp ON dp.d = g.day
        ORDER BY day""")

    # Payment mix, last 7 days
    d["payments_7d"] = rows(cur, """
        SELECT payment_type, sum(total) AS amount, count(*) AS n
        FROM sales WHERE datetime >= current_date - 6
        GROUP BY payment_type ORDER BY amount DESC""")

    # Top products, last 7 days (unnest items_json)
    d["top_products_7d"] = rows(cur, """
        SELECT item->>'name' AS name,
               sum((item->>'total')::numeric) AS revenue,
               sum((item->>'qty')::numeric)   AS qty
        FROM sales, jsonb_array_elements(items_json) AS item
        WHERE datetime >= current_date - 6
        GROUP BY 1 ORDER BY revenue DESC LIMIT 7""")

    # Stock with status
    d["stock"] = rows(cur, """
        SELECT p.id, p.name, p.family,
               coalesce(s.carton_qty,0)  AS shop_c,
               coalesce(s.sleeve_qty,0)  AS shop_s,
               coalesce(dp.carton_qty,0) AS depot_c,
               coalesce(dp.sleeve_qty,0) AS depot_s,
               p.price_carton
        FROM products p
        LEFT JOIN stock_shop  s  ON s.product_id  = p.id
        LEFT JOIN stock_depot dp ON dp.product_id = p.id
        ORDER BY (coalesce(s.carton_qty,0) + coalesce(dp.carton_qty,0)), p.name""")
    for r in d["stock"]:
        tot = r["shop_c"] + r["depot_c"]
        r["status"] = ("rupture" if tot == 0
                       else "bas" if r["shop_c"] < threshold else "ok")
    d["stock_alerts"] = sum(1 for r in d["stock"] if r["status"] != "ok")
    d["low_stock_threshold"] = threshold

    # Credit per customer
    d["credit_customers"] = rows(cur, """
        SELECT c.customer_id, c.name, c.phone,
               coalesce(sum(t.amount) FILTER (WHERE t.paid_status='UNPAID'),0) AS unpaid,
               count(t.*) FILTER (WHERE t.paid_status='UNPAID') AS open_tickets,
               max(t.datetime) AS last_activity
        FROM credit_customers c
        LEFT JOIN credit_transactions t ON t.customer_id = c.customer_id
        GROUP BY c.customer_id, c.name, c.phone
        ORDER BY unpaid DESC""")

    # Live feeds
    d["recent_sales"] = rows(cur, """
        SELECT sale_id, datetime, total, payment_type, sale_source,
               jsonb_array_length(items_json) AS n_items,
               items_json->0->>'name' AS first_item
        FROM sales ORDER BY datetime DESC LIMIT 12""")

    d["recent_restock"] = rows(cur, """
        SELECT "timestamp", product_name, location, type, carton_qty
        FROM restock_history ORDER BY "timestamp" DESC LIMIT 8""")

    # Merged notification feed: POS notifications + web alert engine
    pos_notifs = rows(cur, """
        SELECT 'pos-' || id AS id, category, title, body, severity,
               "timestamp" AS ts, 'pos' AS source
        FROM notifications ORDER BY "timestamp" DESC LIMIT 15""")
    web_notifs = rows(cur, """
        SELECT 'web-' || id AS id, category, title, body, severity,
               created_at AS ts, 'web' AS source
        FROM web_notifications ORDER BY created_at DESC LIMIT 20""")
    feed = sorted(pos_notifs + web_notifs,
                  key=lambda n: str(n["ts"]), reverse=True)[:25]
    d["notif_feed"] = feed

    seen = one(cur, "SELECT last_seen_at FROM web_user_state "
                    "WHERE username = %s", (user["username"],))
    d["notif_last_seen"] = str(seen["last_seen_at"]) if seen else None

    d["withdrawals_recent"] = rows(cur, """
        SELECT datetime, amount, reason FROM withdrawals
        ORDER BY datetime DESC LIMIT 5""")

    # Role gating — the admin role gets operations only: no per-customer
    # credit details, no withdrawals. Stripped server-side, not just hidden.
    if user["role"] != "superadmin":
        d["credit_customers"] = []
        d["withdrawals_recent"] = []
        d["restricted"] = True

    # jsonify decimals/datetimes
    return json.loads(json.dumps(d, default=str))


# ── Static frontend ──────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))

app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")),
          name="static")
