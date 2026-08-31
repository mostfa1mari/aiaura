"use strict";

const $ = (id) => document.getElementById(id);

function cfgGet(k, d = "") { try { return localStorage.getItem(k) || d; } catch { return d; } }
function cfgSet(k, v) { try { localStorage.setItem(k, v); } catch {} }
function cfgDel(k) { try { localStorage.removeItem(k); } catch {} }
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

const state = { asset: null, assets: [], expiry: null, lastResult: null };
let pickerRefresh = null;

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

// ---------- Asset categories (Pocket Option-style grouping) ----------
const CAT_LABEL = {
  currency: "Currencies", cryptocurrency: "Crypto", crypto: "Crypto",
  commodity: "Commodities", stock: "Stocks", index: "Indices", indices: "Indices",
};
const CAT_ORDER = ["currency", "cryptocurrency", "crypto", "commodity", "stock", "index", "indices"];
const catRank = (c) => { const i = CAT_ORDER.indexOf((c || "").toLowerCase()); return i < 0 ? 99 : i; };
const catLabel = (c) => CAT_LABEL[(c || "").toLowerCase()] || ((c || "Other").charAt(0).toUpperCase() + (c || "other").slice(1));

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    if (h.auth_required && !cfg.token) { setStatus("down", "token"); openSetup("This server requires an access token."); return false; }
    if (!h.connected) { setStatus("down", "offline"); return false; }
    setStatus(h.status === "GOOD" ? "good" : "degraded", h.status.toLowerCase());
    return true;
  } catch (e) {
    setStatus("down", "offline");
    if (!cfg.base) openSetup("Can't reach a server. Enter your AI AURA server URL.");
    return false;
  }
}

async function loadExpiries() {
  const { expiries } = await api("/api/expiries");
  const box = $("expiryChips");
  box.innerHTML = "";
  const saved = parseInt(cfgGet("aiaura_expiry"), 10);   // remembered choice
  let selIdx = expiries.indexOf(saved);
  if (selIdx < 0) selIdx = 0;                            // fall back to first if unset/gone
  expiries.forEach((s, i) => {
    const chip = document.createElement("button");
    chip.className = "chip"; chip.type = "button"; chip.setAttribute("role", "radio");
    chip.textContent = fmtExpiry(s);
    const isSel = i === selIdx;
    chip.setAttribute("aria-checked", isSel ? "true" : "false");
    if (isSel) state.expiry = s;
    chip.addEventListener("click", () => {
      [...box.children].forEach((c) => c.setAttribute("aria-checked", "false"));
      chip.setAttribute("aria-checked", "true");
      state.expiry = s;
      cfgSet("aiaura_expiry", String(s));               // persist across reloads/navigation
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
  cfgSet("aiaura_asset", symbol);          // remember choice across reloads/navigation
  updateAssetLabel(symbol);
  api("/api/subscribe", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ asset: symbol }) }).catch(() => {});
}

async function refreshAssets() {
  const { assets, count } = await api("/api/assets");
  state.assets = assets;
  $("analyzeBtn").disabled = assets.length === 0;
  $("controlsHint").textContent = `${count} OTC assets available`;
  if (state.asset) updateAssetLabel(state.asset);
  if (!$("assetSheet").classList.contains("hidden")) renderAssetList($("assetSearch").value);
  return assets;
}
async function loadAssets() {
  try {
    const assets = await refreshAssets();
    if (assets.length && !state.asset) {
      const saved = cfgGet("aiaura_asset");
      const has = (sym) => assets.some((a) => a.symbol === sym);
      const pick = (saved && has(saved)) ? saved
        : (has("EURUSD_otc") ? "EURUSD_otc" : assets[0].symbol);
      setAsset(pick);
    }
  } catch (e) {
    $("analyzeBtn").disabled = true;
    $("assetBtnLabel").textContent = "Unavailable";
    $("controlsHint").textContent = `Market data unavailable — ${e.message}.`;
  }
}

// ---------- Asset picker (grouped, keyboard-aware) ----------
function renderAssetList(filter) {
  const list = $("assetList");
  const q = (filter || "").trim().toLowerCase();
  const items = state.assets.filter((a) =>
    !q || a.symbol.toLowerCase().includes(q) || (a.name || "").toLowerCase().includes(q));
  list.innerHTML = "";
  if (!items.length) {
    const e = document.createElement("div"); e.className = "asset-empty"; e.textContent = "No matching assets";
    list.appendChild(e); return;
  }
  // group by category, ordered PO-style
  const groups = {};
  items.forEach((a) => { (groups[a.category || "other"] ||= []).push(a); });
  const cats = Object.keys(groups).sort((a, b) => catRank(a) - catRank(b) || a.localeCompare(b));
  const frag = document.createDocumentFragment();
  cats.forEach((cat) => {
    const h = document.createElement("div"); h.className = "asset-group"; h.textContent = catLabel(cat);
    frag.appendChild(h);
    groups[cat].sort((x, y) => x.symbol.localeCompare(y.symbol)).forEach((a) => {
      const row = document.createElement("div");
      row.className = "asset-row"; row.setAttribute("role", "option");
      row.setAttribute("aria-selected", a.symbol === state.asset ? "true" : "false");
      const sym = document.createElement("span"); sym.className = "sym"; sym.textContent = shortSym(a.symbol) + " OTC";
      const pay = document.createElement("span");
      pay.className = "pay" + (a.payout != null && a.payout >= 80 ? " high" : "");
      pay.textContent = a.payout != null ? a.payout + "%" : "—";
      row.append(sym, pay);
      row.addEventListener("click", () => { setAsset(a.symbol); closeSheet(); });
      frag.appendChild(row);
    });
  });
  list.appendChild(frag);
}

function applyViewport() {
  const vv = window.visualViewport, sheet = $("assetSheet");
  if (!vv || sheet.classList.contains("hidden")) return;
  const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
  sheet.style.bottom = kb + "px";                       // sit above the keyboard
  sheet.style.maxHeight = (vv.height - 12) + "px";      // fit entirely in view
}
function clearViewport() { const s = $("assetSheet"); s.style.bottom = ""; s.style.maxHeight = ""; }

function openSheet() {
  if (!state.assets.length) return;
  $("sheetScrim").classList.remove("hidden");
  const sheet = $("assetSheet");
  sheet.classList.remove("hidden", "leaving"); sheet.classList.add("enter");
  $("assetSearch").value = "";
  renderAssetList("");
  refreshAssets().catch(() => {});
  applyViewport();
  if (pickerRefresh) clearInterval(pickerRefresh);
  pickerRefresh = setInterval(() => { refreshAssets().catch(() => {}); }, 8000);  // live payouts
  requestAnimationFrame(() => {
    const sel = $("assetList").querySelector('[aria-selected="true"]');
    if (sel) sel.scrollIntoView({ block: "center" });
  });
}
function closeSheet() {
  if (pickerRefresh) { clearInterval(pickerRefresh); pickerRefresh = null; }
  $("assetSearch").blur();
  const sheet = $("assetSheet");
  sheet.classList.remove("enter"); sheet.classList.add("leaving");
  $("sheetScrim").classList.add("hidden");
  const done = () => { sheet.classList.add("hidden"); sheet.classList.remove("leaving"); clearViewport(); };
  sheet.addEventListener("transitionend", done, { once: true });
  setTimeout(done, 340);
}

async function analyze() {
  if (!state.asset || !state.expiry) return;
  const btn = $("analyzeBtn");
  btn.classList.add("loading"); btn.disabled = true;
  $("controlsHint").textContent = "";
  try {
    const r = await api("/api/analyze", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset: state.asset, expiry_s: state.expiry }) });
    showResult(r, /*persist*/ true);
  } catch (e) {
    $("controlsHint").textContent = `Analyze failed: ${e.message}`;
  } finally {
    btn.classList.remove("loading"); btn.disabled = false;
  }
}

function showResult(r, persist) {
  state.lastResult = r;
  if (persist) cfgSet("aiaura_last_signal", JSON.stringify(r));
  const card = $("resultCard");
  card.classList.remove("hidden", "buy", "sell", "wait", "explore");
  void card.offsetWidth;

  // Always a clear BUY/SELL (green/red). Confidence is shown honestly, never hides it.
  const side = (r.signal || "BUY").toUpperCase();
  card.classList.add(side.toLowerCase());

  const conf = (r.confidence != null ? r.confidence : (r.strength || 0));
  const pct = Math.round(conf * 100);
  const be = r.break_even != null ? Math.round(r.break_even * 100) : null;
  const support = r.confidence_support || 0;
  const tier = r.tier || "moderate";
  const tierWord = tier === "strong" ? "Strong" : (tier === "low" ? "Low" : "Moderate");

  const setText = (id, txt) => { const el = $(id); if (el) el.textContent = txt; };

  setText("signalBig", side);
  setText("signalAsset", shortSym(r.asset) + " OTC");
  setText("signalExpiry", fmtExpiry(r.expiry_s));
  setText("meterLabel", `Win chance · ${tierWord}`);
  $("strengthBar").style.width = pct + "%"; setText("strengthVal", pct + "%");

  // One short, honest context line.
  const parts = [];
  if (be != null) parts.push(`needs ${be}% to profit`);
  parts.push(support ? `${support} past outcome${support === 1 ? "" : "s"} for this asset`
                     : "learning this asset — grade to improve");
  setText("confMeta", parts.join(" · "));

  const rz = $("resultReasons");
  const reasons = Array.isArray(r.reasons) ? r.reasons : [];
  if (rz) {
    if (reasons.length) {
      rz.innerHTML = reasons.map((x) => `<span>${x}</span>`).join(" ");
      rz.classList.remove("hidden");
    } else rz.classList.add("hidden");
  }

  // Always gradeable — WIN/LOSS is how the confidence learns.
  const fb = $("feedbackBox"); if (fb) fb.classList.remove("hidden");

  $("mAgreement").textContent = Math.round((r.agreement || 0) * 100) + "%";
  $("mRegime").textContent = (r.regime || "—").replace(/_/g, " ");
  $("mSuff").textContent = Math.round((r.data_sufficiency || 0) * 100) + "%";
  $("mEntry").textContent = r.entry_price != null ? r.entry_price : "—";
  $("mPayout").textContent = r.payout != null ? r.payout + "%" : "—";
  const sim = r.historical_similarity;
  $("mSim").textContent = sim ? `${sim.leans} ${Math.round(sim.directional_rate * 100)}% · ${sim.n_neighbors}${sim.confident ? "" : " ~"}` : "—";
  const lv = r.latency_viability;
  $("mLatV").textContent = lv ? lv.verdict.replace(/_/g, " ") : "—";
  $("mLatency").textContent = r.prediction_latency_ms != null ? Math.round(r.prediction_latency_ms) + " ms" : "—";
  $("mModel").textContent = r.model_version || "—";

  const note = $("resultNote");
  if (r.note) { note.textContent = r.note; note.classList.remove("hidden"); } else note.classList.add("hidden");
  const t = new Date((r.created_at || Date.now() / 1000) * 1000);
  $("signalTime").textContent = "Generated " + t.toLocaleTimeString();

  const answered = !!(r._answered);
  $("winBtn").disabled = answered; $("lossBtn").disabled = answered;
  $("feedbackDone").classList.toggle("hidden", !answered);
  if (persist) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function sendFeedback(result) {
  const r = state.lastResult;
  if (!r || r._answered) return;
  $("winBtn").disabled = true; $("lossBtn").disabled = true;
  try {
    await api("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ result, prediction: r }) });
    r._answered = true;
    cfgDel("aiaura_last_signal");                 // answered -> stop persisting
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
    $("sTotal").textContent = s.settled;             // only settled (answered) signals count
    $("sWin").textContent = s.wins;
    $("sLoss").textContent = s.losses;
    $("sRate").textContent = s.win_rate != null ? Math.round(s.win_rate * 100) + "%" : "—";
  } catch { /* ignore */ }
}

function restoreLastSignal() {
  const raw = cfgGet("aiaura_last_signal");
  if (!raw) return;
  try {
    const r = JSON.parse(raw);
    if (r && r.signal && r.asset) showResult(r, /*persist*/ false);  // still answerable
  } catch { cfgDel("aiaura_last_signal"); }
}

// ---------- Connection setup ----------
function openSetup(msg) {
  $("cfgBase").value = cfg.base; $("cfgToken").value = cfg.token;
  const m = $("setupMsg"); m.classList.remove("ok"); m.textContent = msg || "";
  $("setupScrim").classList.remove("hidden");
  const s = $("setupSheet"); s.classList.remove("hidden", "leaving"); s.classList.add("enter");
}
function closeSetup() {
  const s = $("setupSheet"); s.classList.remove("enter"); s.classList.add("leaving");
  $("setupScrim").classList.add("hidden");
  const done = () => { s.classList.add("hidden"); s.classList.remove("leaving"); };
  s.addEventListener("transitionend", done, { once: true }); setTimeout(done, 340);
}
async function saveSetup() {
  cfg.base = $("cfgBase").value.trim(); cfg.token = $("cfgToken").value.trim();
  cfgSet("aiaura_base", cfg.base); cfgSet("aiaura_token", cfg.token);
  const m = $("setupMsg"); m.classList.remove("ok"); m.textContent = "Connecting…";
  const ok = await refreshHealth();
  if (ok) { m.classList.add("ok"); m.textContent = "Connected ✓"; loadAssets(); refreshStats(); setTimeout(closeSetup, 500); }
  else m.textContent = "Still can't connect — check the URL and token.";
}

function preventZoom() {
  document.addEventListener("gesturestart", (e) => e.preventDefault());
  document.addEventListener("gesturechange", (e) => e.preventDefault());
  let last = 0;
  const INTERACTIVE = "button, a, input, textarea, select, [role='button'], [role='option'], [role='radio'], .chip, .asset-row";
  document.addEventListener("touchend", (e) => {
    const n = Date.now();
    // Double-tap-zoom prevention must NOT swallow taps on buttons/controls — on
    // iOS a preventDefault here cancels the click, which was eating WIN/LOSS taps.
    const interactive = e.target && e.target.closest && e.target.closest(INTERACTIVE);
    if (n - last <= 300 && e.cancelable && !interactive) e.preventDefault();
    last = n;
  }, { passive: false });
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
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", applyViewport);
    window.visualViewport.addEventListener("scroll", applyViewport);
  }

  restoreLastSignal();                 // keep the last signal answerable after navigation
  loadExpiries().catch(() => {});
  loadAssets();
  refreshHealth();
  refreshStats();
  setInterval(refreshHealth, 15000);
  setInterval(() => { if ($("assetSheet").classList.contains("hidden")) refreshAssets().catch(() => {}); }, 30000);

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js").catch(() => {});
}

document.addEventListener("DOMContentLoaded", init);
