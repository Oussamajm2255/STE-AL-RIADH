# STE AL RIADH — Tableau de bord cloud

Read-only monitoring web app for the STE AL RIADH POS (boutique + dépôt).
Reads the Railway PostgreSQL schema fed in real time by **AbdouProSync.exe**
running on the shop's PC.

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

`DATABASE_URL` env var overrides the built-in connection string.

## Deploy on Railway (production)

1. Railway → the project that already hosts the Postgres DB →
   **New service → GitHub repo** → select `STE-AL-RIADH`.
2. In the service **Variables**, add
   `DATABASE_URL = ${{Postgres.DATABASE_URL}}` (internal URL — faster, free egress).
3. Settings → **Generate Domain**. Done — `railway.json` provides the start
   command and the `/healthz` health check.

## Demo data

The DB currently holds **fake demo data** (`scripts/seed_demo_data.py`) so the
dashboard is fully populated before production starts. The first run of
AbdouProSync.exe on the shop PC **replaces all of it automatically** — every
table loads with DELETE + INSERT from the real CSVs.

To re-seed the demo: `python scripts/seed_demo_data.py`.
