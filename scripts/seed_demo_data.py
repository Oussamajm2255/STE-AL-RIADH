# ═══════════════════════════════════════════════════════════════════════════
#  seed_demo_data.py — Fill the cloud schema with realistic DEMO data.
#
#  TEMPORARY: this simulates the production POS until the real
#  AbdouProSync.exe starts publishing. Running the real sync replaces
#  everything automatically (each file loads with DELETE + INSERT).
#
#  Usage:  python scripts/seed_demo_data.py
#          (reads DATABASE_URL env var, falls back to the Railway public URL)
# ═══════════════════════════════════════════════════════════════════════════

import json
import os
import random
import uuid
from datetime import datetime, timedelta, date

import psycopg2
import psycopg2.extras

DB = os.environ.get("DATABASE_URL") or (
    "postgresql://postgres:NjrXhNHOXVEPQEaqEHlLljZqoRLLuKEL"
    "@centerbeam.proxy.rlwy.net:28214/railway"
)

random.seed(42)   # deterministic demo

def rid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"

NOW = datetime.now()
TODAY = date.today()

# ── Catalog: animal-feed & packaging shop, 5 familles ────────────────────────
PRODUCTS = [
    # (name, family, price_carton, price_sleeve, sleeves_per_carton)
    ("Aliment Poulet Chair 25kg",  "Production",     43.50, 0,    1),
    ("Aliment Ponte Extra 25kg",   "Production",     46.00, 0,    1),
    ("Aliment Dinde Croissance",   "Production",     52.00, 0,    1),
    ("Aliment Lapin Premium",      "Production",     38.50, 0,    1),
    ("Mais Concassé 30kg",         "Production",     34.00, 0,    1),
    ("Orge Entier 30kg",           "Production",     31.00, 0,    1),
    ("Son de Blé 25kg",            "Production",     22.50, 0,    1),
    ("Levure 20x500g",             "Additifs",       43.50, 2.50, 20),
    ("Vitamine AD3E 24x100ml",     "Additifs",       96.00, 4.50, 24),
    ("Anti-Coccidien 12x250g",     "Additifs",       78.00, 7.00, 12),
    ("Calcium Coquille 15kg",      "Additifs",       18.00, 0,    1),
    ("Zinou 40x250g",              "Additifs",       23.00, 0.70, 40),
    ("Grand Sac 50kg (x100)",      "Grands Sacs",    85.00, 1.00, 100),
    ("Grand Sac 25kg (x100)",      "Grands Sacs",    65.00, 0.75, 100),
    ("Sac Tissé Vert (x100)",      "Grands Sacs",    72.00, 0.85, 100),
    ("Sachet B2 (x500)",           "Petits Sachets", 19.00, 0.05, 500),
    ("Sachet B4 (x500)",           "Petits Sachets", 26.00, 0.07, 500),
    ("Papier Hrayri 10kg",         "Petits Sachets", 80.00, 8.00, 10),
    ("Vanoise 24pcs",              "Divers",         28.80, 1.40, 24),
    ("Ficelle Agricole 5kg",       "Divers",         41.00, 0,    1),
]

CUSTOMERS = [
    ("Haithem Ben Nejah", "94201684"), ("Skander Trabelsi", "55321870"),
    ("Ferme Sidi Thabet", "71544902"), ("Moez Poulailler",  "22876340"),
    ("Ridha Ben Amor",    "98123456"), ("Coopérative El Fejja", "70998812"),
    ("Sami Volailles",    "52449018"), ("Ferme Bou Salem",  "97665231"),
]

def main():
    conn = psycopg2.connect(DB, connect_timeout=15)
    cur = conn.cursor()

    cur.execute("""TRUNCATE products, stock_shop, stock_depot, sales,
        credit_customers, credit_transactions, restock_history, shifts,
        withdrawals, app_settings, notifications, sync_meta RESTART IDENTITY""")

    # ── products ────────────────────────────────────────────────────────────
    prods = []
    for name, fam, pc, ps, spc in PRODUCTS:
        pid = rid("prod")
        prods.append({"id": pid, "name": name, "family": fam,
                      "pc": pc, "ps": ps, "spc": spc})
        cur.execute(
            "INSERT INTO products (id, name, price_carton, price_sleeve, "
            "sleeves_per_carton, family) VALUES (%s,%s,%s,%s,%s,%s)",
            (pid, name, pc, ps if spc > 1 else None, spc, fam))

    # ── stock: mostly healthy, a few alerts so the dashboard has a story ────
    for i, p in enumerate(prods):
        if i == 4:            shop_c, depot_c = 0, 0            # RUPTURE totale
        elif i == 9:          shop_c, depot_c = 0, 14           # rupture boutique
        elif i in (2, 13):    shop_c, depot_c = 2, 3            # stock bas
        elif i == 17:         shop_c, depot_c = 3, 0            # dépôt vide
        else:
            shop_c, depot_c = random.randint(6, 40), random.randint(15, 120)
        shop_s = random.randint(0, p["spc"] - 1) if p["spc"] > 1 else 0
        cur.execute("INSERT INTO stock_shop VALUES (%s,%s,%s)",
                    (p["id"], shop_c, shop_s))
        cur.execute("INSERT INTO stock_depot VALUES (%s,%s,%s)",
                    (p["id"], depot_c, random.randint(0, 5) if p["spc"] > 1 else 0))

    # ── customers ───────────────────────────────────────────────────────────
    custs = []
    for name, phone in CUSTOMERS:
        cid = rid("cust")
        custs.append({"id": cid, "name": name})
        cur.execute("INSERT INTO credit_customers VALUES (%s,%s,%s,%s)",
                    (cid, name, phone, random.random() < 0.25))

    # ── 45 days of sales + credit ───────────────────────────────────────────
    weights = [10, 2, 1]  # Cash, Check, Credit
    n_sales = 0
    credit_rows = []
    for back in range(44, -1, -1):
        d = TODAY - timedelta(days=back)
        # weekly rhythm: markets Wed/Sat busier; today partial
        base = 14 + (6 if d.weekday() in (2, 5) else 0)
        count = random.randint(base - 4, base + 6)
        if back == 0:
            count = max(4, int(count * min(1.0, NOW.hour / 19)))
        for _ in range(count):
            h = random.choices(range(7, 20),
                               weights=[2,5,7,8,6,4,3,4,6,7,6,4,2])[0]
            ts = datetime.combine(d, datetime.min.time()) + timedelta(
                hours=h, minutes=random.randint(0, 59), seconds=random.randint(0, 59))
            if back == 0 and ts > NOW:
                ts = NOW - timedelta(minutes=random.randint(2, 90))
            source = "DEPOT" if random.random() < 0.30 else "BOUTIQUE"
            items, total = [], 0.0
            for p in random.sample(prods, random.randint(1, 4)):
                if source == "DEPOT" or p["spc"] == 1 or random.random() < 0.6:
                    qty = random.randint(1, 12 if source == "DEPOT" else 3)
                    price, typ = p["pc"], "carton"
                else:
                    qty = random.randint(1, min(p["spc"], 15))
                    price, typ = p["ps"], "sleeve"
                line = round(qty * price, 2)
                total += line
                items.append({"product_id": p["id"], "name": p["name"],
                              "type": typ, "qty": qty, "price": price,
                              "total": line})
            total = round(total, 2)
            pay = random.choices(["Cash", "Check", "Credit"], weights=weights)[0]
            if pay == "Credit" and source != "DEPOT":
                pay = "Cash"     # credit mostly on depot bulk orders
            sid = rid("dpot" if source == "DEPOT" else "sale")
            discount = round(total * 0.03, 2) if total > 400 and random.random() < 0.3 else None
            if discount:
                total = round(total - discount, 2)
            cur.execute(
                "INSERT INTO sales VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (sid, ts.isoformat(), psycopg2.extras.Json(items),
                 total, discount, pay, source))
            n_sales += 1
            if pay == "Credit":
                c = random.choice(custs)
                paid = "PAID" if back > 21 and random.random() < 0.65 else "UNPAID"
                credit_rows.append(
                    (rid("trx"), c["id"], sid, total, ts.isoformat(), paid))
    for row in credit_rows:
        cur.execute("INSERT INTO credit_transactions VALUES (%s,%s,%s,%s,%s,%s)", row)

    # ── restock history ─────────────────────────────────────────────────────
    for back in range(40, -1, -3):
        d = NOW - timedelta(days=back, hours=random.randint(1, 6))
        for p in random.sample(prods, random.randint(2, 4)):
            kind = "RESTOCK" if random.random() < 0.6 else "TRANSFER"
            cur.execute(
                'INSERT INTO restock_history ("timestamp", product_id, '
                "product_name, location, type, carton_qty, sleeve_qty) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (d.isoformat(), p["id"], p["name"],
                 "DEPOT" if kind == "RESTOCK" else "SHOP",
                 kind, random.randint(5, 40), 0))

    # ── shifts (one per day, fond declared) ─────────────────────────────────
    for back in range(14, 0, -1):
        d = TODAY - timedelta(days=back)
        end = datetime.combine(d, datetime.min.time()) + timedelta(hours=13, minutes=30)
        cur.execute("INSERT INTO shifts VALUES (%s,%s,%s,%s)",
                    (d, end.isoformat(), random.choice([50, 80, 100]),
                     random.choice([50, 80, 100])))

    # ── withdrawals ─────────────────────────────────────────────────────────
    for back, amount, why in [(18, 200, "Achat gasoil camion"),
                              (11, 150, "Avance ouvrier"),
                              (6, 320, "Paiement fournisseur"),
                              (2, 100, "Frais divers"),
                              (0, 80,  "Avance Salah")]:
        ts = NOW - timedelta(days=back, hours=random.randint(1, 5))
        cur.execute("INSERT INTO withdrawals VALUES (%s,%s,%s,%s)",
                    (rid("wdr"), ts.isoformat(), amount, why))

    # ── settings ────────────────────────────────────────────────────────────
    cur.execute("INSERT INTO app_settings (id, data) VALUES (1, %s)",
                (psycopg2.extras.Json({
                    "shop_name": "STE AL RIADH",
                    "address": "Sidi Fathallah, Tunis",
                    "phone": "+216 55 087 618",
                    "currency": "DT",
                    "low_stock_threshold": 5}),))

    # ── notifications ───────────────────────────────────────────────────────
    notifs = [
        (8,  "stock",   "danger",  "Rupture de stock",
         "Mais Concassé 30kg : 0 carton en boutique et au dépôt"),
        (26, "stock",   "warning", "Stock bas",
         "Aliment Dinde Croissance : 2 cartons restants en boutique"),
        (55, "restock", "success", "Chargement effectué",
         "Aliment Poulet Chair : +25 cartons → Dépôt"),
        (140, "credit", "warning", "Crédit élevé",
         "Ferme Sidi Thabet dépasse 900 DT d'encours"),
        (260, "shift",  "info",    "Shift 1 clôturé",
         "Fond de caisse : 100.00 DT"),
        (1300, "backup", "success", "Sauvegarde automatique",
         "Sauvegarde quotidienne créée avec succès"),
        (2700, "restock", "success", "Transfert effectué",
         "Levure : 10 cartons Dépôt → Boutique"),
        (4100, "credit", "success", "Paiement reçu",
         "Haithem Ben Nejah a réglé 435.00 DT"),
    ]
    for minutes, cat, sev, title, body in notifs:
        ts = NOW - timedelta(minutes=minutes)
        cur.execute(
            'INSERT INTO notifications (id, category, title, body, severity, '
            '"timestamp", read, dismissed) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
            (rid("notif"), cat, title, body, sev, ts.isoformat(),
             minutes > 300, False))

    # ── sync_meta (freshness the dashboard displays) ────────────────────────
    for f in ["products.csv", "stock_shop.csv", "stock_depot.csv", "sales.csv",
              "credit_customers.csv", "credit_transactions.csv",
              "restock_history.csv", "shifts.csv", "withdrawals.csv",
              "settings.json", "notifications.json"]:
        cur.execute("INSERT INTO sync_meta VALUES (%s, now(), %s, %s)",
                    (f, random.randint(5, 300), "demo" + uuid.uuid4().hex[:6]))

    conn.commit()
    cur.execute("SELECT count(*) FROM sales")
    print(f"Seeded: {len(prods)} products, {n_sales} sales "
          f"({cur.fetchone()[0]} in db), {len(credit_rows)} credit trx, "
          f"{len(custs)} customers")
    conn.close()

if __name__ == "__main__":
    main()
