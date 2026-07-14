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

import json
import os
from contextlib import contextmanager
from datetime import datetime

import psycopg2
import psycopg2.extras
import psycopg2.pool
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

DATABASE_URL = os.environ.get("DATABASE_URL") or (
    "postgresql://postgres:NjrXhNHOXVEPQEaqEHlLljZqoRLLuKEL"
    "@centerbeam.proxy.rlwy.net:28214/railway"
)

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


# ── API ──────────────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    try:
        with db() as cur:
            one(cur, "SELECT 1 AS ok")
        return {"ok": True}
    except Exception:
        return JSONResponse({"ok": False}, status_code=503)


@app.get("/api/dashboard")
def dashboard():
    try:
        with db() as cur:
            return build_dashboard(cur)
    except psycopg2.Error:
        raise HTTPException(503, "database unavailable")


def build_dashboard(cur):
    d = {}

    settings = one(cur, "SELECT data FROM app_settings WHERE id = 1")
    d["settings"] = settings["data"] if settings else {}
    threshold = int(d["settings"].get("low_stock_threshold", 5) or 5)

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

    d["notifications"] = rows(cur, """
        SELECT id, category, title, body, severity, "timestamp", read
        FROM notifications ORDER BY "timestamp" DESC LIMIT 15""")

    d["withdrawals_recent"] = rows(cur, """
        SELECT datetime, amount, reason FROM withdrawals
        ORDER BY datetime DESC LIMIT 5""")

    # jsonify decimals/datetimes
    return json.loads(json.dumps(d, default=str))


# ── Static frontend ──────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))

app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")),
          name="static")
