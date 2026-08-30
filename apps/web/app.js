"use strict";

const $ = (id) => document.getElementById(id);

function cfgGet(k, d = "") { try { return localStorage.getItem(k) || d; } catch { return d; } }
function cfgSet(k, v) { try { localStorage.setItem(k, v); } catch {} }
const cfg = { base: cfgGet("aiaura_base"), token: cfgGet("aiaura_token") };

const api = (path, opts = {}) => {
  const headers = Object.assign({}, opts.headers || {});
  if (cfg.token) headers["Authorization"] = "Bearer " + cfg.token;
  const url = (cfg.base ? cfg.base.replace(/\/$/, "") : "") + path;
  return fetch(url, Object.assign({}, opts, { headers })).then(async (r) => {
    const body = await r.json().catch(() => ({}));
    if (r.status === 401) { openSetup("This server needs an access token."); throw new Error(body.detail || "unauthorized"); }
    if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
    return body;
  });
};

const state = { asset: null, assets: [], expiry: null, lastSignalId: null };

const shortSym = (s) => s.replace(/_otc$/, "").replace(/-/g, "");
function fmtExpiry(s) {
  if (s < 60) return `${s} SEC`;
  const m = s / 60;
  return `${m % 1 === 0 ? m : m.toFixed(1)} MIN`;
}
function setStatus(stateName, text) {
  $("statusPill").dataset.state = stateName;
  $("statusText").textContent = text;
}

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    if (h.auth_required && !cfg.token) { setStatus("down", "token"); openSetup("This server requires an access token."); return false; }
    if (!h.connected) { setStatus("down", "offline"); return false; }
    setStatus(h.status === "GOOD" ? "good" : "degraded", h.status.toLowerCase());
    return true;
  } catch (e) {
    setStatus("down", "offline");
    // Unreachable and no server configured yet -> guide the user to setup.
    if (!cfg.base) openSetup("Can't reach a server. Enter your AI AURA server URL.");
    return false;
  }
}

// ---------- Connection setup ----------
function openSetup(msg) {
  $("cfgBase").value = cfg.base;
  $("cfgToken").value = cfg.token;
  const m = $("setupMsg"); m.classList.remove("ok"); m.textContent = msg || "";
  $("setupScrim").classList.remove("hidden");
  const s = $("setupSheet"); s.classList.remove("hidden", "leaving"); s.classList.add("enter");
}
function closeSetup() {
  const s = $("setupSheet");
  s.classList.remove("enter"); s.classList.add("leaving");
  $("setupScrim").classList.add("hidden");
  const done = () => { s.classList.add("hidden"); s.classList.remove("leaving"); };
  s.addEventListener("transitionend", done, { once: true });
  setTimeout(done, 340);
}
async function saveSetup() {
  cfg.base = $("cfgBase").value.trim();
  cfg.token = $("cfgToken").value.trim();
  cfgSet("aiaura_base", cfg.base);
  cfgSet("aiaura_token", cfg.token);
  const m = $("setupMsg"); m.classList.remove("ok"); m.textContent = "Connecting…";
  const ok = await refreshHealth();
  if (ok) {
    m.classList.add("ok"); m.textContent = "Connected ✓";
    loadAssets(); refreshStats();
    setTimeout(closeSetup, 500);
  } else {
    m.textContent = "Still can't connect — check the URL and token.";
  }
}

async function loadExpiries() {
  const { expiries } = await api("/api/expiries");
  const box = $("expiryChips");
  box.innerHTML = "";
  expiries.forEach((s, i) => {
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.type = "button";
    chip.setAttribute("role", "radio");
    chip.textContent = fmtExpiry(s);
    chip.setAttribute("aria-checked", i === 0 ? "true" : "false");
    if (i === 0) state.expiry = s;
    chip.addEventListener("click", () => {
      [...box.children].forEach((c) => c.setAttribute("aria-checked", "false"));
      chip.setAttribute("aria-checked", "true");
      state.expiry = s;
    });
    box.appendChild(chip);
  });
}

function updateAssetLabel(symbol) {
  const a = state.assets.find((x) => x.symbol === symbol);
  const payout = a && a.payout != null ? ` · ${a.payout}%` : "";
  $("assetBtnLabel").textContent = `${shortSym(symbol)} OTC${payout}`;
}

function setAsset(symbol) {
  state.asset = symbol;
  updateAssetLabel(symbol);
  // Pre-subscribe so ANALYZE is instant (fire-and-forget).
  api("/api/subscribe", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ asset: symbol }),
  }).catch(() => {});
}

// Fetch the live catalog and refresh payouts WITHOUT changing the selection
// (Pocket Option payouts move over time — keep the shown numbers current).
async function refreshAssets() {
  const { assets, count } = await api("/api/assets");
  state.assets = assets;
  $("analyzeBtn").disabled = assets.length === 0;
  $("controlsHint").textContent = `${count} OTC assets available`;
  if (state.asset) updateAssetLabel(state.asset);
  // if the picker is open, re-render it with fresh payouts
  if (!$("assetSheet").classList.contains("hidden")) renderAssetList($("assetSearch").value);
  return assets;
}

async function loadAssets() {
  try {
    const assets = await refreshAssets();
    if (assets.length && !state.asset) {
      const eur = assets.find((a) => a.symbol === "EURUSD_otc");
      setAsset(eur ? "EURUSD_otc" : assets[0].symbol);
    }
  } catch (e) {
    $("analyzeBtn").disabled = true;
    $("assetBtnLabel").textContent = "Unavailable";
    $("controlsHint").textContent = `Market data unavailable — ${e.message}. Check the server / PO_SSID.`;
  }
}

// ---------- Asset picker bottom sheet ----------
function renderAssetList(filter) {
  const list = $("assetList");
  const q = (filter || "").trim().toLowerCase();
  const items = state.assets.filter((a) =>
    !q || a.symbol.toLowerCase().includes(q) || (a.name || "").toLowerCase().includes(q));
  list.innerHTML = "";
  if (!items.length) {
    const e = document.createElement("div");
    e.className = "asset-empty";
    e.textContent = "No matching assets";
    list.appendChild(e);
    return;
  }
  const frag = document.createDocumentFragment();
  items.forEach((a) => {
    const row = document.createElement("div");
    row.className = "asset-row";
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", a.symbol === state.asset ? "true" : "false");
    const sym = document.createElement("span");
    sym.className = "sym";
    sym.textContent = shortSym(a.symbol) + " OTC";
    const pay = document.createElement("span");
    pay.className = "pay" + (a.payout != null && a.payout >= 80 ? " high" : "");
    pay.textContent = a.payout != null ? a.payout + "%" : "—";
    row.append(sym, pay);
    row.addEventListener("click", () => { setAsset(a.symbol); closeSheet(); });
    frag.appendChild(row);
  });
  list.appendChild(frag);
}

function openSheet() {
  if (!state.assets.length) return;
  $("sheetScrim").classList.remove("hidden");
  const sheet = $("assetSheet");
  sheet.classList.remove("hidden", "leaving");
  sheet.classList.add("enter");
  $("assetSearch").value = "";
  renderAssetList("");
  refreshAssets().catch(() => {});  // ensure payouts are current when browsing
  // scroll selected into view
  requestAnimationFrame(() => {
    const sel = $("assetList").querySelector('[aria-selected="true"]');
    if (sel) sel.scrollIntoView({ block: "center" });
  });
}
function closeSheet() {
  const sheet = $("assetSheet");
  sheet.classList.remove("enter");
  sheet.classList.add("leaving");
  $("sheetScrim").classList.add("hidden");
  const done = () => { sheet.classList.add("hidden"); sheet.classList.remove("leaving"); };
  sheet.addEventListener("transitionend", done, { once: true });
  setTimeout(done, 340);
}

async function analyze() {
  if (!state.asset || !state.expiry) return;
  const btn = $("analyzeBtn");
  btn.classList.add("loading"); btn.disabled = true;
  $("controlsHint").textContent = "";
  try {
    const r = await api("/api/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset: state.asset, expiry_s: state.expiry }),
    });
    showResult(r);
  } catch (e) {
    $("controlsHint").textContent = `Analyze failed: ${e.message}`;
  } finally {
    btn.classList.remove("loading"); btn.disabled = false;
  }
}

function showResult(r) {
  state.lastSignalId = r.signal_id;
  const card = $("resultCard");
  card.classList.remove("hidden", "buy", "sell");
  // restart entrance animation
  void card.offsetWidth;
  card.classList.add(r.signal.toLowerCase());

  $("signalBig").textContent = r.signal;
  $("signalAsset").textContent = shortSym(r.asset) + " OTC";
  $("signalExpiry").textContent = fmtExpiry(r.expiry_s);

  const pct = Math.round(r.strength * 100);
  $("strengthBar").style.width = pct + "%";
  $("strengthVal").textContent = pct + "%";
  $("mAgreement").textContent = Math.round(r.agreement * 100) + "%";
  $("mRegime").textContent = (r.regime || "—").replace(/_/g, " ");
  $("mSuff").textContent = Math.round(r.data_sufficiency * 100) + "%";
  $("mEntry").textContent = r.entry_price != null ? r.entry_price : "—";
  $("mPayout").textContent = r.payout != null ? r.payout + "%" : "—";
  const sim = r.historical_similarity;
  $("mSim").textContent = sim ? `${sim.leans} ${Math.round(sim.directional_rate * 100)}% · ${sim.n_neighbors}${sim.confident ? "" : " ~"}` : "—";
  const lv = r.latency_viability;
  $("mLatV").textContent = lv ? lv.verdict.replace(/_/g, " ") : "—";
  $("mLatency").textContent = r.prediction_latency_ms != null ? Math.round(r.prediction_latency_ms) + " ms" : "—";
  $("mModel").textContent = r.model_version || "—";

  const note = $("resultNote");
  if (r.note) { note.textContent = r.note; note.classList.remove("hidden"); }
  else note.classList.add("hidden");

  const t = new Date((r.created_at || Date.now() / 1000) * 1000);
  $("signalTime").textContent = "Generated " + t.toLocaleTimeString();

  $("winBtn").disabled = false;
  $("lossBtn").disabled = false;
  $("feedbackDone").classList.add("hidden");
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function sendFeedback(result) {
  if (!state.lastSignalId) return;
  $("winBtn").disabled = true; $("lossBtn").disabled = true;
  try {
    await api("/api/feedback", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ signal_id: state.lastSignalId, result }),
    });
    $("feedbackDone").classList.remove("hidden");
    refreshStats();
  } catch (e) {
    $("winBtn").disabled = false; $("lossBtn").disabled = false;
    $("controlsHint").textContent = `Feedback failed: ${e.message}`;
  }
}

async function refreshStats() {
  try {
    const s = await api("/api/stats");
    $("sTotal").textContent = s.total;
    $("sWin").textContent = s.wins;
    $("sLoss").textContent = s.losses;
    $("sRate").textContent = s.win_rate != null ? Math.round(s.win_rate * 100) + "%" : "—";
  } catch { /* ignore */ }
}

function preventZoom() {
  // Block iOS pinch-zoom and double-tap zoom (belt-and-suspenders with the
  // viewport meta + touch-action).
  document.addEventListener("gesturestart", (e) => e.preventDefault());
  document.addEventListener("gesturechange", (e) => e.preventDefault());
  let lastTouch = 0;
  document.addEventListener("touchend", (e) => {
    const now = Date.now();
    if (now - lastTouch <= 300 && e.cancelable) e.preventDefault();
    lastTouch = now;
  }, { passive: false });
  // Block context menu (long-press) globally.
  document.addEventListener("contextmenu", (e) => e.preventDefault());
}

function init() {
  preventZoom();
  $("analyzeBtn").addEventListener("click", analyze);
  $("winBtn").addEventListener("click", () => sendFeedback("WIN"));
  $("lossBtn").addEventListener("click", () => sendFeedback("LOSS"));
  $("statusPill").addEventListener("click", refreshHealth);
  $("settingsBtn").addEventListener("click", () => openSetup(""));
  $("setupScrim").addEventListener("click", closeSetup);
  $("setupHandle").addEventListener("click", closeSetup);
  $("cfgSave").addEventListener("click", saveSetup);
  $("assetBtn").addEventListener("click", openSheet);
  $("sheetClose").addEventListener("click", closeSheet);
  $("sheetScrim").addEventListener("click", closeSheet);
  $("sheetHandle").addEventListener("click", closeSheet);
  $("assetSearch").addEventListener("input", (e) => renderAssetList(e.target.value));

  loadExpiries().catch(() => {});
  loadAssets();
  refreshHealth();
  refreshStats();
  setInterval(refreshHealth, 15000);
  setInterval(() => { refreshAssets().catch(() => {}); }, 30000);  // keep payouts fresh

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  }
}

document.addEventListener("DOMContentLoaded", init);
