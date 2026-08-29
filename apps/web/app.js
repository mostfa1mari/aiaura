"use strict";

const $ = (id) => document.getElementById(id);
const api = (path, opts) => fetch(path, opts).then(async (r) => {
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
});

const state = { asset: null, expiry: null, lastSignalId: null, expiries: [] };

function fmtExpiry(s) {
  if (s < 60) return `${s} SEC`;
  const m = s / 60;
  return `${m % 1 === 0 ? m : m.toFixed(1)} MIN`;
}

function setStatus(stateName, text) {
  const pill = $("statusPill");
  pill.dataset.state = stateName;
  $("statusText").textContent = text;
}

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    if (!h.connected) { setStatus("down", "offline"); return false; }
    setStatus(h.status === "GOOD" ? "good" : "degraded", h.status.toLowerCase());
    return true;
  } catch (e) {
    setStatus("down", "offline");
    return false;
  }
}

async function loadExpiries() {
  const { expiries } = await api("/api/expiries");
  state.expiries = expiries;
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

async function loadAssets() {
  const hint = $("controlsHint");
  try {
    const { assets, count } = await api("/api/assets");
    const sel = $("assetSelect");
    sel.innerHTML = "";
    assets.forEach((a) => {
      const opt = document.createElement("option");
      opt.value = a.symbol;
      const payout = a.payout != null ? ` · ${a.payout}%` : "";
      opt.textContent = `${a.symbol.replace("_otc", "")} OTC${payout}`;
      sel.appendChild(opt);
    });
    if (assets.length) {
      state.asset = assets[0].symbol;
      // prefer EURUSD_otc if present
      const eur = assets.find((a) => a.symbol === "EURUSD_otc");
      if (eur) { sel.value = "EURUSD_otc"; state.asset = "EURUSD_otc"; }
    }
    sel.addEventListener("change", () => { state.asset = sel.value; });
    $("analyzeBtn").disabled = assets.length === 0;
    hint.textContent = `${count} OTC assets available`;
  } catch (e) {
    $("analyzeBtn").disabled = true;
    hint.textContent = `Market data unavailable — ${e.message}. Check PO_SSID in .env and restart the server.`;
  }
}

async function analyze() {
  if (!state.asset || !state.expiry) return;
  const btn = $("analyzeBtn");
  btn.classList.add("loading");
  btn.disabled = true;
  $("controlsHint").textContent = "";
  try {
    const r = await api("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset: state.asset, expiry_s: state.expiry }),
    });
    showResult(r);
  } catch (e) {
    $("controlsHint").textContent = `Analyze failed: ${e.message}`;
  } finally {
    btn.classList.remove("loading");
    btn.disabled = false;
  }
}

function showResult(r) {
  state.lastSignalId = r.signal_id;
  const card = $("resultCard");
  card.classList.remove("hidden", "buy", "sell");
  card.classList.add(r.signal.toLowerCase());

  $("signalBig").textContent = r.signal;
  $("signalAsset").textContent = r.asset.replace("_otc", "") + " OTC";
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
  else { note.classList.add("hidden"); }

  const t = new Date((r.created_at || Date.now() / 1000) * 1000);
  $("signalTime").textContent = "Generated " + t.toLocaleTimeString();

  // reset feedback
  $("winBtn").disabled = false;
  $("lossBtn").disabled = false;
  $("feedbackDone").classList.add("hidden");

  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function sendFeedback(result) {
  if (!state.lastSignalId) return;
  $("winBtn").disabled = true;
  $("lossBtn").disabled = true;
  try {
    await api("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ signal_id: state.lastSignalId, result }),
    });
    $("feedbackDone").classList.remove("hidden");
    refreshStats();
  } catch (e) {
    $("winBtn").disabled = false;
    $("lossBtn").disabled = false;
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
  } catch (e) { /* ignore */ }
}

function init() {
  $("analyzeBtn").addEventListener("click", analyze);
  $("winBtn").addEventListener("click", () => sendFeedback("WIN"));
  $("lossBtn").addEventListener("click", () => sendFeedback("LOSS"));
  $("statusPill").addEventListener("click", refreshHealth);

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
