"use strict";

const $ = (id) => document.getElementById(id);
const api = (path, opts) => fetch(path, opts).then(async (r) => {
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
});

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
    if (!h.connected) { setStatus("down", "offline"); return false; }
    setStatus(h.status === "GOOD" ? "good" : "degraded", h.status.toLowerCase());
    return true;
  } catch { setStatus("down", "offline"); return false; }
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

function setAsset(symbol) {
  state.asset = symbol;
  const a = state.assets.find((x) => x.symbol === symbol);
  const payout = a && a.payout != null ? ` · ${a.payout}%` : "";
  $("assetBtnLabel").textContent = `${shortSym(symbol)} OTC${payout}`;
  // Pre-subscribe so ANALYZE is instant (fire-and-forget).
  api("/api/subscribe", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ asset: symbol }),
  }).catch(() => {});
}

async function loadAssets() {
  const hint = $("controlsHint");
  try {
    const { assets, count } = await api("/api/assets");
    state.assets = assets;
    if (assets.length) {
      const eur = assets.find((a) => a.symbol === "EURUSD_otc");
      setAsset(eur ? "EURUSD_otc" : assets[0].symbol);
    }
    $("analyzeBtn").disabled = assets.length === 0;
    hint.textContent = `${count} OTC assets available`;
  } catch (e) {
    $("analyzeBtn").disabled = true;
    $("assetBtnLabel").textContent = "Unavailable";
    hint.textContent = `Market data unavailable — ${e.message}. Check PO_SSID in .env and restart.`;
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

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  }
}

document.addEventListener("DOMContentLoaded", init);
