# STE AL RIADH — Tableau de bord cloud

Read-only monitoring web app for the STE AL RIADH POS (boutique + dépôt).
Reads the Railway PostgreSQL schema fed in real time by **AbdouProSync.exe**
running on the shop's PC.

## Access & roles

Login is required. Two roles, configured **entirely through environment
variables** (no credentials in code):

| Variable | Format | Role |
|---|---|---|
| `DASH_SUPERADMIN` | `username:password` | Full dashboard |
| `DASH_ADMIN` | `username:password` | Operations only — credit details and withdrawals are stripped **server-side** |

Sessions are signed HttpOnly cookies (HMAC-SHA256, 7 days). Optional
`SECRET_KEY` env var pins the signing key (otherwise derived from
`DATABASE_URL`). Login has an 8-failures/60s brute-force brake.

## Notifications system

- **Bell + badge** in the topbar with per-user unread counts and
  "Tout marquer comme lu".
- The feed merges **POS notifications** (synced from the shop) with alerts
  from the **built-in engine** (background thread, one pass/minute, tagged
  `AUTO`): stock ruptures, low boutique stock, credit exposure ≥ 1500 DT,
  big sales ≥ 1000 DT, and sync offline > 15 min. Deduplicated per subject
  with cooldowns, stored in `web_notifications`.
- Toasts pop on new sales and new notifications (8 s polling).

## What the manager sees

- **EN DIRECT pill** — data freshness from `sync_meta` (live / stale / offline)
- **KPIs** — today's revenue vs yesterday, tickets, boutique/dépôt split,
  outstanding credit, stock alerts
- **Charts** — 14-day revenue (boutique vs dépôt), payment mix, top products
- **Stock** — searchable, family filters, RUPTURE / BAS / OK status pills
- **Crédits** — per-customer outstanding balances
- **Activité** — live sales feed, shop notifications, withdrawals, restocks
- **Toasts** — pop up on new sales and new notifications (8s polling)
- Mobile responsive · light & dark theme

## Stack

FastAPI + psycopg2 (one aggregate endpoint `/api/dashboard`), vanilla
HTML/CSS/JS frontend — no CDN, no build step, hand-rolled SVG charts.

## Run locally

```bash
pip install -r requirements.txt
uvicorn server:app --reload
# http://127.0.0.1:8000
```

The `DATABASE_URL` env var is **required** — there are no credentials in the
code. The app refuses to start without it.

## Deploy on Railway (production)

1. Railway → the project that already hosts the Postgres DB →
   **New service → GitHub repo** → select `STE-AL-RIADH`.
2. In the service **Variables**, add:
   - `DATABASE_URL = ${{Postgres.DATABASE_URL}}` (internal URL — faster, free egress)
   - `DASH_SUPERADMIN = <username:password>`
   - `DASH_ADMIN = <username:password>`
   - `SECRET_KEY = <random hex>` (optional but recommended)
3. Settings → **Generate Domain**. Done — `railway.json` provides the start
   command and the `/healthz` health check.

## Demo data

The DB currently holds **fake demo data** (`scripts/seed_demo_data.py`) so the
dashboard is fully populated before production starts. The first run of
AbdouProSync.exe on the shop PC **replaces all of it automatically** — every
table loads with DELETE + INSERT from the real CSVs.

To re-seed the demo: `python scripts/seed_demo_data.py`.
