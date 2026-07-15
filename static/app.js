/* ═══════════════════════════════════════════════════════════════════════
   STE AL RIADH — Pilotage · client logic
   Polls /api/dashboard every 8s, renders everything, toasts on new events.
   ═══════════════════════════════════════════════════════════════════════ */

"use strict";

const POLL_MS = 8000;
const $ = (s) => document.querySelector(s);

let DATA = null;
let USER = null;
let lastSaleId = null;
let lastNotifId = null;
let firstLoad = true;
let lastOkFetch = Date.now();
let stockFilter = { family: "Toutes", q: "" };
let pollTimer = null;

/* ── formatting ──────────────────────────────────────────────────────── */

const CUR = () => (DATA?.settings?.currency || "DT");
const fmtN = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });
const fmtM = new Intl.NumberFormat("fr-FR",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function money(v, compact = false) {
  const n = Number(v) || 0;
  if (compact && Math.abs(n) >= 10000) return fmtN.format(Math.round(n));
  return compact ? fmtN.format(Math.round(n)) : fmtM.format(n);
}

function relTime(iso) {
  if (!iso) return "—";
  const t = new Date(String(iso).replace(" ", "T"));
  const s = Math.max(0, (Date.now() - t.getTime()) / 1000);
  if (s < 50) return "à l'instant";
  if (s < 3600) return `il y a ${Math.round(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.round(s / 3600)} h`;
  return t.toLocaleDateString("fr-FR", { day: "2-digit", month: "short" });
}

function dayLabel(iso) {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" });
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ── theme ───────────────────────────────────────────────────────────── */

function initTheme() {
  const saved = new URLSearchParams(location.search).get("theme")
    || localStorage.getItem("theme");
  const dark = saved ? saved === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.classList.toggle("dark", dark);
  $("#theme-btn").onclick = () => {
    const isDark = document.documentElement.classList.toggle("dark");
    localStorage.setItem("theme", isDark ? "dark" : "light");
    if (DATA) renderCharts();          // re-render svg with new tokens
  };
}

/* ── auth ────────────────────────────────────────────────────────────── */

const ROLE_LABEL = { superadmin: "Super Admin", admin: "Admin" };

function clearUI() {
  // Wipe every rendered container so no data from a previous session
  // lingers in the DOM across logout / user switch.
  DATA = null;
  lastSaleId = null;
  lastNotifId = null;
  $("#page").dataset.loading = "1";
  ["#kpi-grid", "#chart-revenue", "#chart-payments", "#chart-top",
   "#stock-table tbody", "#credit-table tbody", "#withdrawals-feed",
   "#restock-feed", "#sales-feed", "#notif-feed", "#notif-panel-list",
   "#family-chips"].forEach((sel) => {
    const el = $(sel);
    if (el) el.innerHTML = "";
  });
  $("#bell-badge").hidden = true;
}

function showLogin(msg) {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  USER = null;
  clearUI();
  document.body.classList.remove("role-admin");
  $("#user-chip").hidden = true;
  $("#notif-panel").hidden = true;
  $("#login-screen").hidden = false;
  const err = $("#login-error");
  err.hidden = !msg;
  if (msg) err.textContent = msg;
  setTimeout(() => $("#login-user").focus(), 50);
}

function enterApp(user) {
  USER = user;
  clearUI();
  $("#login-screen").hidden = true;
  $("#user-chip").hidden = false;
  $("#user-name").textContent = user.username;
  $("#user-role").textContent = ROLE_LABEL[user.role] || user.role;
  $("#user-avatar").textContent = (user.username[0] || "A").toUpperCase();
  document.body.classList.toggle("role-admin", user.role !== "superadmin");
  firstLoad = true;
  tick();
}

async function boot() {
  try {
    const r = await fetch("/api/me", { cache: "no-store" });
    if (r.ok) { enterApp(await r.json()); return; }
  } catch { /* server unreachable — fall through to login */ }
  showLogin();
}

async function doLogin(e) {
  e.preventDefault();
  const btn = $("#login-submit");
  btn.disabled = true;
  btn.textContent = "Connexion…";
  try {
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: $("#login-user").value,
        password: $("#login-pass").value,
      }),
    });
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail;
      showLogin(detail || "Connexion impossible");
      return;
    }
    $("#login-error").hidden = true;
    enterApp(await r.json());
  } catch {
    showLogin("Serveur injoignable — réessayez");
  } finally {
    btn.disabled = false;
    btn.textContent = "Se connecter";
  }
}

async function doLogout() {
  try { await fetch("/api/logout", { method: "POST" }); } catch {}
  showLogin();
}

/* ── fetch loop ──────────────────────────────────────────────────────── */

async function tick() {
  try {
    const r = await fetch("/api/dashboard", { cache: "no-store" });
    if (r.status === 401) { showLogin("Session expirée — reconnectez-vous"); return; }
    if (!r.ok) throw new Error(r.status);
    const fresh = await r.json();
    detectEvents(fresh);
    DATA = fresh;
    lastOkFetch = Date.now();
    $("#offline-banner").hidden = true;
    renderAll();
    firstLoad = false;
  } catch {
    const mins = Math.round((Date.now() - lastOkFetch) / 60000);
    if (mins >= 1) {
      $("#offline-banner").hidden = false;
      $("#offline-since").textContent = `depuis ${mins} min`;
    }
    renderLivePill();  // degrade the pill even without data
  } finally {
    pollTimer = setTimeout(tick, POLL_MS);
  }
}

function detectEvents(fresh) {
  const newestSale = fresh.recent_sales?.[0];
  if (!firstLoad && newestSale && lastSaleId && newestSale.sale_id !== lastSaleId) {
    const news = [];
    for (const s of fresh.recent_sales) {
      if (s.sale_id === lastSaleId) break;
      news.push(s);
    }
    news.slice(0, 3).reverse().forEach((s) => toast("t-sale",
      `Nouvelle vente — ${money(s.total)} ${CUR()}`,
      `${s.sale_source === "DEPOT" ? "Dépôt" : "Boutique"} · ${s.n_items} article(s) · ${payLabel(s.payment_type)}`));
    if (news.length > 3)
      toast("t-sale", `+${news.length - 3} autres ventes`, "");
  }
  if (newestSale) lastSaleId = newestSale.sale_id;

  const newestNotif = fresh.notif_feed?.[0];
  if (!firstLoad && newestNotif && lastNotifId && newestNotif.id !== lastNotifId) {
    let shown = 0;
    for (const n of fresh.notif_feed) {
      if (n.id === lastNotifId || shown >= 3) break;
      toast(n.severity === "danger" ? "t-danger"
        : n.severity === "warning" ? "t-warning" : "",
        n.title, n.body);
      shown++;
    }
  }
  if (newestNotif) lastNotifId = newestNotif.id;
}

/* ── notification center ─────────────────────────────────────────────── */

function parseTs(iso) {
  return new Date(String(iso || "").replace(" ", "T")).getTime() || 0;
}

function unseenCount() {
  if (!DATA?.notif_feed) return 0;
  const seen = DATA.notif_last_seen ? parseTs(DATA.notif_last_seen) : 0;
  return DATA.notif_feed.filter((n) => parseTs(n.ts) > seen).length;
}

function renderBell() {
  const n = unseenCount();
  const badge = $("#bell-badge");
  badge.hidden = n === 0;
  badge.textContent = n > 99 ? "99+" : n;
  if (!$("#notif-panel").hidden) renderNotifPanel();
}

function renderNotifPanel() {
  const list = DATA?.notif_feed || [];
  const seen = DATA?.notif_last_seen ? parseTs(DATA.notif_last_seen) : 0;
  const now = Date.now();
  const DAY = 86400000;

  function bucket(ts) {
    const age = now - ts;
    if (age < DAY) return "Aujourd'hui";
    if (age < 2 * DAY) return "Hier";
    return "Plus tôt";
  }

  const groups = {};
  list.forEach((n) => {
    const key = bucket(parseTs(n.ts));
    if (!groups[key]) groups[key] = [];
    groups[key].push(n);
  });

  const order = ["Aujourd'hui", "Hier", "Plus tôt"];
  let html = "";
  order.forEach((label) => {
    const items = groups[label];
    if (!items?.length) return;
    html += `<li class="notif-group-label">${label}</li>`;
    items.forEach((n) => {
      html += `
        <li class="sev-${esc(n.severity)} ${parseTs(n.ts) > seen ? "unseen" : ""}">
          <div class="ico">${CAT_ICO[n.category] || "📌"}</div>
          <div class="body">
            <div class="t1">${esc(n.title)}${n.source === "web"
              ? '<span class="src-tag">AUTO</span>' : ""}</div>
            <div class="t2">${esc(n.body)}</div>
          </div>
          <div class="right"><div class="when">${relTime(n.ts)}</div></div>
        </li>`;
    });
  });

  $("#notif-panel-list").innerHTML = html;

  const empty = !list.length;
  $("#notif-panel-list").hidden = empty;
  $("#notif-empty").hidden = !empty;

  const count = unseenCount();
  $("#notif-count").hidden = count === 0;
  $("#notif-count").textContent = count > 99 ? "99+" : count;
}

function openNotifPanel() {
  const panel = $("#notif-panel");
  const backdrop = $("#notif-backdrop");
  panel.hidden = false;
  backdrop.hidden = false;
  // Force reflow before adding .show for the transition to work
  panel.offsetHeight;
  panel.classList.add("show");
  backdrop.classList.add("show");
  document.body.style.overflow = "hidden";
  renderNotifPanel();
}

function closeNotifPanel() {
  const panel = $("#notif-panel");
  const backdrop = $("#notif-backdrop");
  panel.classList.remove("show");
  backdrop.classList.remove("show");
  document.body.style.overflow = "";
  // Hide after transition ends
  setTimeout(() => {
    if (!panel.classList.contains("show")) {
      panel.hidden = true;
      backdrop.hidden = true;
    }
  }, 320);
}

function toggleNotifPanel(force) {
  const panel = $("#notif-panel");
  const open = force !== undefined ? force : panel.hidden || !panel.classList.contains("show");
  if (open) openNotifPanel();
  else closeNotifPanel();
}

async function markAllSeen() {
  try {
    await fetch("/api/notifications/seen", { method: "POST" });
    if (DATA) DATA.notif_last_seen = new Date().toISOString();
    renderBell();
    renderNotifPanel();
  } catch {}
}

function toast(cls, title, body) {
  const el = document.createElement("div");
  el.className = `toast ${cls}`;
  el.innerHTML = `<b>${esc(title)}</b><span>${esc(body)}</span>`;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .4s"; }, 6000);
  setTimeout(() => el.remove(), 6500);
}

/* ── render root ─────────────────────────────────────────────────────── */

function renderAll() {
  $("#page").dataset.loading = "0";
  const st = DATA.settings || {};
  $("#shop-name").textContent = st.shop_name || "STE AL RIADH";
  $("#shop-sub").textContent = st.address || "Tableau de bord";
  renderLivePill();
  renderKPIs();
  renderCharts();
  renderStock();
  renderCredit();
  renderFeeds();
  renderBell();
  const badge = $("#nav-stock-badge");
  badge.hidden = !DATA.stock_alerts;
  badge.textContent = DATA.stock_alerts;
  $("#foot-sync").textContent =
    `Dernière synchronisation : ${relTime(DATA.last_sync)}`;
}

function renderLivePill() {
  const pill = $("#live-pill");
  pill.classList.remove("live", "stale", "down");
  if (!DATA?.last_sync) { $("#live-text").textContent = "en attente…"; return; }
  const age = (Date.now() - new Date(DATA.last_sync.replace(" ", "T")).getTime()) / 1000;
  if (age < 180) {
    pill.classList.add("live");
    $("#live-text").textContent = "EN DIRECT";
  } else if (age < 1800) {
    pill.classList.add("stale");
    $("#live-text").textContent = `synchro ${relTime(DATA.last_sync)}`;
  } else {
    pill.classList.add("down");
    $("#live-text").textContent = `hors ligne · ${relTime(DATA.last_sync)}`;
  }
}

/* ── KPIs ────────────────────────────────────────────────────────────── */

function payLabel(p) {
  return p === "Cash" ? "Espèces" : p === "Check" ? "Chèque" : "Crédit";
}

function deltaHTML(today, yest) {
  const t = Number(today) || 0, y = Number(yest) || 0;
  if (!y) return `<span class="kpi-foot">hier : ${money(y, true)}</span>`;
  const pct = ((t - y) / y) * 100;
  const cls = pct >= 0 ? "delta-up" : "delta-down";
  const arrow = pct >= 0 ? "▲" : "▼";
  return `<span class="${cls}">${arrow} ${Math.abs(pct).toFixed(0)}%</span>
          <span>vs hier</span>`;
}

function renderKPIs() {
  const t = DATA.today, y = DATA.yesterday, c = DATA.credit;
  const cur = CUR();
  const tiles = [
    { label: "Recette du jour", accent: "var(--s1)",
      value: `${money(t.revenue, true)} <small>${cur}</small>`,
      foot: deltaHTML(t.revenue, y.revenue) },
    { label: "Tickets aujourd'hui", accent: "var(--s1)",
      value: fmtN.format(t.tickets),
      foot: `<span>panier moyen ${money(t.avg_ticket, true)} ${cur}</span>` },
    { label: "Boutique", accent: "var(--s1)",
      value: `${money(t.boutique, true)} <small>${cur}</small>`,
      foot: `<span>ventes comptoir</span>` },
    { label: "Dépôt", accent: "var(--s2)",
      value: `${money(t.depot, true)} <small>${cur}</small>`,
      foot: `<span>ventes en gros</span>` },
    { label: "Crédit en cours", accent: "var(--warning)",
      value: `${money(c.unpaid_total, true)} <small>${cur}</small>`,
      foot: `<span>${c.unpaid_count} ticket(s) impayé(s)</span>` },
    { label: "Alertes stock", accent: DATA.stock_alerts ? "var(--critical)" : "var(--good)",
      value: fmtN.format(DATA.stock_alerts),
      foot: DATA.stock_alerts
        ? `<span>produit(s) à surveiller</span>`
        : `<span class="delta-up">✓ stock sain</span>` },
  ];
  $("#kpi-grid").innerHTML = tiles.map((k) => `
    <div class="kpi" style="--kpi-accent:${k.accent}">
      <div class="kpi-label">${k.label}</div>
      <div class="kpi-value">${k.value}</div>
      <div class="kpi-foot">${k.foot}</div>
    </div>`).join("");
}

/* ── charts ──────────────────────────────────────────────────────────── */

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function renderCharts() {
  renderRevenue();
  renderPayments();
  renderTopProducts();
}

function renderRevenue() {
  const data = DATA.revenue_14d || [];
  const host = $("#chart-revenue");
  const s1 = cssVar("--s1"), s2 = cssVar("--s2");
  const W = 720, H = 240, padL = 46, padR = 8, padT = 12, padB = 26;
  const iw = W - padL - padR, ih = H - padT - padB;
  const totals = data.map((d) => (+d.boutique) + (+d.depot));
  const grand = totals.reduce((a, b) => a + b, 0);
  $("#rev-total-sub").textContent =
    `${money(grand, true)} ${CUR()} cumulés sur 14 jours`;

  const maxRaw = Math.max(...totals, 1);
  const step = niceStep(maxRaw / 3);
  const maxV = Math.ceil(maxRaw / step) * step;

  const n = data.length;
  const slot = iw / n;
  const bw = Math.min(34, slot * 0.62);
  const x0 = (i) => padL + i * slot + (slot - bw) / 2;
  const yv = (v) => padT + ih - (v / maxV) * ih;

  let g = "";
  for (let v = step; v <= maxV; v += step) {
    g += `<line class="gridline" x1="${padL}" y1="${yv(v)}" x2="${W - padR}" y2="${yv(v)}"/>
          <text class="axis-label" x="${padL - 7}" y="${yv(v) + 3.5}" text-anchor="end">${money(v, true)}</text>`;
  }

  let bars = "", hover = "";
  data.forEach((d, i) => {
    const b = +d.boutique, dp = +d.depot, tot = b + dp;
    const x = x0(i);
    const yvia = yv(b);                         // top of boutique segment
    const yTop = yv(tot);
    // boutique (bottom, anchored to baseline; rounded only when topmost)
    if (b > 0) {
      bars += roundedRect(x, yvia, bw, padT + ih - yvia, dp > 0 ? 0 : 4, s1);
    }
    // dépôt on top with a 2px surface gap
    if (dp > 0) {
      const hSeg = Math.max(0, yvia - yTop - (b > 0 ? 2 : 0));
      bars += roundedRect(x, yTop, bw, hSeg, 4, s2);
    }
    // x labels every 2 days
    if (i % 2 === (n - 1) % 2) {
      bars += `<text class="axis-label" x="${x + bw / 2}" y="${H - 8}" text-anchor="middle">${dayLabel(d.day)}</text>`;
    }
    hover += `<rect x="${padL + i * slot}" y="${padT}" width="${slot}" height="${ih}"
      fill="transparent" data-i="${i}"/>`;
  });

  host.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Chiffre d'affaires 14 jours">
      ${g}
      <line class="baseline" x1="${padL}" y1="${padT + ih}" x2="${W - padR}" y2="${padT + ih}"/>
      ${bars}${hover}
    </svg>`;

  const tt = $("#tooltip");
  host.querySelectorAll("rect[data-i]").forEach((r) => {
    r.addEventListener("mousemove", (e) => {
      const d = data[+r.dataset.i];
      const tot = (+d.boutique) + (+d.depot);
      tt.innerHTML = `<b>${dayLabel(d.day)}</b>
        <div class="tt-row"><i class="swatch" style="background:${s1}"></i>Boutique&nbsp;<b>${money(d.boutique)} ${CUR()}</b></div>
        <div class="tt-row"><i class="swatch" style="background:${s2}"></i>Dépôt&nbsp;<b>${money(d.depot)} ${CUR()}</b></div>
        <div class="tt-row">Total&nbsp;<b>${money(tot)} ${CUR()}</b></div>`;
      placeTooltip(e);
    });
    r.addEventListener("mouseleave", () => { tt.hidden = true; });
  });
}

function niceStep(x) {
  const p = Math.pow(10, Math.floor(Math.log10(Math.max(x, 1))));
  for (const m of [1, 2, 2.5, 5, 10]) if (x <= m * p) return m * p;
  return 10 * p;
}

function roundedRect(x, y, w, h, r, fill) {
  if (h <= 0) return "";
  r = Math.min(r, h, w / 2);
  return `<path fill="${fill}" d="M${x},${y + h} L${x},${y + r}
    Q${x},${y} ${x + r},${y} L${x + w - r},${y}
    Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z"/>`;
}

function placeTooltip(e) {
  const tt = $("#tooltip");
  tt.hidden = false;
  const pad = 14, r = tt.getBoundingClientRect();
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = e.clientY - r.height - pad;
  tt.style.left = x + "px"; tt.style.top = y + "px";
}

function renderPayments() {
  const host = $("#chart-payments");
  const list = DATA.payments_7d || [];
  const total = list.reduce((a, p) => a + (+p.amount), 0) || 1;
  const colors = { Cash: cssVar("--s1"), Check: cssVar("--s2"), Credit: cssVar("--s3") };
  const order = ["Cash", "Check", "Credit"];
  const sorted = order.map((k) => list.find((p) => p.payment_type === k))
    .filter(Boolean);
  host.innerHTML = `
    <div class="seg-bar">${sorted.map((p) => `
      <div class="seg" style="width:${(100 * p.amount / total).toFixed(1)}%;
        background:${colors[p.payment_type]}"></div>`).join("")}
    </div>
    <div class="seg-legend">${sorted.map((p) => `
      <div class="row">
        <i class="swatch" style="background:${colors[p.payment_type]}"></i>
        ${payLabel(p.payment_type)} · ${p.n} tickets
        <span class="val">${money(p.amount, true)} ${CUR()}
          · ${(100 * p.amount / total).toFixed(0)}%</span>
      </div>`).join("")}
    </div>`;
}

function renderTopProducts() {
  const host = $("#chart-top");
  const list = DATA.top_products_7d || [];
  const max = Math.max(...list.map((p) => +p.revenue), 1);
  host.innerHTML = list.map((p) => `
    <div class="hbar-row" title="${esc(p.name)} — ${fmtN.format(p.qty)} unités">
      <div class="hbar-name">${esc(p.name)}</div>
      <div class="hbar-track"><div class="hbar-fill"
           style="width:${(100 * p.revenue / max).toFixed(1)}%"></div></div>
      <div class="hbar-val">${money(p.revenue, true)}</div>
    </div>`).join("") || `<p class="card-sub">Aucune vente sur 7 jours</p>`;
}

/* ── stock ───────────────────────────────────────────────────────────── */

const PILL = {
  ok:      `<span class="pill pill-ok">✓ OK</span>`,
  bas:     `<span class="pill pill-bas">▲ BAS</span>`,
  rupture: `<span class="pill pill-rupture">● RUPTURE</span>`,
};

function renderStock() {
  const all = DATA.stock || [];
  $("#stock-sub").textContent =
    `${all.length} produits · seuil d'alerte : ${DATA.low_stock_threshold} cartons · ${DATA.stock_alerts} alerte(s)`;

  const families = ["Toutes", ...new Set(all.map((r) => r.family || "Divers"))];
  $("#family-chips").innerHTML = families.map((f) =>
    `<button class="chip ${f === stockFilter.family ? "active" : ""}"
       data-f="${esc(f)}">${esc(f)}</button>`).join("");
  $("#family-chips").querySelectorAll(".chip").forEach((b) => {
    b.onclick = () => { stockFilter.family = b.dataset.f; renderStock(); };
  });

  const q = stockFilter.q.toLowerCase();
  const rows = all.filter((r) =>
    (stockFilter.family === "Toutes" || (r.family || "Divers") === stockFilter.family)
    && (!q || r.name.toLowerCase().includes(q)));

  $("#stock-table tbody").innerHTML = rows.map((r) => `
    <tr>
      <td class="main">${esc(r.name)}</td>
      <td class="sub">${esc(r.family || "—")}</td>
      <td class="num">${r.shop_c} C${r.shop_s ? ` · ${r.shop_s} P` : ""}</td>
      <td class="num">${r.depot_c} C${r.depot_s ? ` · ${r.depot_s} P` : ""}</td>
      <td class="num main">${r.shop_c + r.depot_c} C</td>
      <td>${PILL[r.status]}</td>
    </tr>`).join("") ||
    `<tr><td colspan="6" class="sub">Aucun produit ne correspond</td></tr>`;
}

/* ── credit ──────────────────────────────────────────────────────────── */

function renderCredit() {
  const list = DATA.credit_customers || [];
  const total = list.reduce((a, c) => a + (+c.unpaid), 0);
  $("#credit-sub").textContent =
    `${money(total, true)} ${CUR()} d'encours total · ${list.filter((c) => +c.unpaid > 0).length} client(s) débiteur(s)`;
  $("#credit-table tbody").innerHTML = list.map((c) => `
    <tr>
      <td class="main">${esc(c.name)}</td>
      <td class="sub">${esc(c.phone || "—")}</td>
      <td class="num">${c.open_tickets}</td>
      <td class="num main" style="color:${+c.unpaid > 0 ? "var(--crit-text)" : "var(--good-text)"}">
        ${money(c.unpaid)} ${CUR()}</td>
    </tr>`).join("");
}

/* ── feeds ───────────────────────────────────────────────────────────── */

const CAT_ICO = { restock: "🚚", stock: "📦", credit: "💰", shift: "🕐",
                  backup: "💾", system: "⚙️", sales: "💵" };

function renderFeeds() {
  $("#sales-feed").innerHTML = (DATA.recent_sales || []).map((s, i) => `
    <li ${i === 0 && !firstLoad ? 'class="fresh"' : ""}>
      <div class="ico">${s.sale_source === "DEPOT" ? "🏭" : "🏪"}</div>
      <div class="body">
        <div class="t1">${s.sale_source === "DEPOT" ? "Dépôt" : "Boutique"}
             · ${payLabel(s.payment_type)}</div>
        <div class="t2">${esc(s.first_item || "")}${s.n_items > 1 ? ` +${s.n_items - 1}` : ""}</div>
      </div>
      <div class="right">
        <div class="amount">${money(s.total)} ${CUR()}</div>
        <div class="when">${relTime(s.datetime)}</div>
      </div>
    </li>`).join("");

  $("#notif-feed").innerHTML = (DATA.notif_feed || []).slice(0, 12).map((n) => `
    <li class="sev-${esc(n.severity)}">
      <div class="ico">${CAT_ICO[n.category] || "📌"}</div>
      <div class="body">
        <div class="t1">${esc(n.title)}${n.source === "web"
          ? '<span class="src-tag">AUTO</span>' : ""}</div>
        <div class="t2">${esc(n.body)}</div>
      </div>
      <div class="right"><div class="when">${relTime(n.ts)}</div></div>
    </li>`).join("");

  $("#withdrawals-feed").innerHTML = (DATA.withdrawals_recent || []).map((w) => `
    <li>
      <div class="ico">💸</div>
      <div class="body"><div class="t1">${esc(w.reason || "Prélèvement")}</div>
        <div class="t2">${relTime(w.datetime)}</div></div>
      <div class="right"><div class="amount">−${money(w.amount)} ${CUR()}</div></div>
    </li>`).join("");

  $("#restock-feed").innerHTML = (DATA.recent_restock || []).map((r) => `
    <li>
      <div class="ico">${r.type === "TRANSFER" ? "🔁" : "🚚"}</div>
      <div class="body">
        <div class="t1">${esc(r.product_name)}</div>
        <div class="t2">${r.type === "TRANSFER" ? "Transfert vers boutique" : "Chargement dépôt"}
             · +${r.carton_qty} cartons</div>
      </div>
      <div class="right"><div class="when">${relTime(r.timestamp)}</div></div>
    </li>`).join("");
}

/* ── section nav highlight ───────────────────────────────────────────── */

function initNav() {
  const links = [...document.querySelectorAll(".section-nav a")];
  const secs = links.map((a) => $(a.getAttribute("href")));
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (e.isIntersecting) {
        links.forEach((l) => l.classList.toggle("active",
          l.getAttribute("href") === "#" + e.target.id));
      }
    });
  }, { rootMargin: "-30% 0px -60% 0px" });
  secs.forEach((s) => io.observe(s));
}

/* ── boot ────────────────────────────────────────────────────────────── */

initTheme();
initNav();
$("#stock-search").addEventListener("input", (e) => {
  stockFilter.q = e.target.value; if (DATA) renderStock();
});
$("#login-form").addEventListener("submit", doLogin);
$("#logout-btn").addEventListener("click", doLogout);
$("#bell-btn").addEventListener("click", (e) => {
  e.stopPropagation(); toggleNotifPanel();
});
$("#notif-close").addEventListener("click", () => toggleNotifPanel(false));
$("#notif-backdrop").addEventListener("click", () => toggleNotifPanel(false));
$("#mark-seen-btn").addEventListener("click", markAllSeen);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#notif-panel").hidden) toggleNotifPanel(false);
});
setInterval(() => { if (DATA) { renderLivePill(); } }, 20000);
boot();
