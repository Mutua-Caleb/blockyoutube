/* BlockYouTube remote controller (PWA).
 *
 * Stores topic + HMAC secret in localStorage. Signs commands with
 * HMAC-SHA256 in the browser via WebCrypto. Subscribes to the status
 * topic via EventSource for the live "online / locked" indicator.
 */

const LS_KEY = "blockyoutube.config.v1";
const DEFAULT_CATEGORIES = [
  "youtube", "facebook", "instagram", "tiktok",
  "twitter", "reddit", "snapchat", "twitch", "netflix",
];

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let config = null;             // {ntfy_base, cmd_topic, status_topic, secret}
let secretKey = null;          // CryptoKey for HMAC
let lastStatus = null;         // last decoded heartbeat
let lastStatusAt = 0;          // epoch ms when we received it
let evtSource = null;          // EventSource for status

// ---------- storage -----------------------------------------------------

function loadConfig() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function saveConfig(cfg) {
  localStorage.setItem(LS_KEY, JSON.stringify(cfg));
}

function clearConfig() {
  localStorage.removeItem(LS_KEY);
}

// ---------- crypto ------------------------------------------------------

function hexToBytes(hex) {
  if (!/^[0-9a-fA-F]*$/.test(hex) || hex.length % 2 !== 0) {
    throw new Error("secret must be an even-length hex string");
  }
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

function bytesToHex(bytes) {
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function importSecret(hex) {
  const keyBytes = hexToBytes(hex);
  return crypto.subtle.importKey(
    "raw",
    keyBytes,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
}

// Canonical JSON (sorted keys, no whitespace) -- must match agent.py.
function canonical(obj) {
  if (obj === null || typeof obj !== "object") return JSON.stringify(obj);
  if (Array.isArray(obj)) {
    return "[" + obj.map(canonical).join(",") + "]";
  }
  const keys = Object.keys(obj).sort();
  return "{" + keys.map((k) => JSON.stringify(k) + ":" + canonical(obj[k])).join(",") + "}";
}

async function signCmd(cmd) {
  const { sig: _drop, ...body } = cmd;
  const data = new TextEncoder().encode(canonical(body));
  const sig = await crypto.subtle.sign("HMAC", secretKey, data);
  return { ...body, sig: bytesToHex(new Uint8Array(sig)) };
}

// ---------- ntfy --------------------------------------------------------

function cmdUrl() {
  return `${config.ntfy_base.replace(/\/+$/, "")}/${encodeURIComponent(config.cmd_topic)}`;
}

function statusSseUrl() {
  return `${config.ntfy_base.replace(/\/+$/, "")}/${encodeURIComponent(config.status_topic)}/sse`;
}

function statusPollUrl() {
  return `${config.ntfy_base.replace(/\/+$/, "")}/${encodeURIComponent(config.status_topic)}/json?poll=1`;
}

async function publish(cmd) {
  const signed = await signCmd(cmd);
  const resp = await fetch(cmdUrl(), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(signed),
  });
  if (!resp.ok) {
    throw new Error(`ntfy POST failed: ${resp.status} ${resp.statusText}`);
  }
  return signed;
}

// ---------- status indicator -------------------------------------------

function setIndicator(state, label) {
  const ind = $("#indicator");
  ind.classList.remove("ok", "warn", "bad", "unknown");
  ind.classList.add(state);
  $("#indicator-text").textContent = label;
}

function classifyHeartbeat() {
  if (!lastStatus) {
    setIndicator("unknown", "no status yet");
    return;
  }
  const ageMs = Date.now() - lastStatusAt;
  const mode = lastStatus.mode;
  let label;
  if (mode === "block_all") label = "locked";
  else if (mode === "allow") {
    const allow = (lastStatus.allow || []).join(", ") || "(none)";
    label = lastStatus.until
      ? `unlocked ${allow} until ${formatUntil(lastStatus.until)}`
      : `unlocked ${allow}`;
  } else label = "unknown mode";

  if (ageMs < 90_000) setIndicator("ok", label);
  else if (ageMs < 5 * 60_000) setIndicator("warn", `${label} (stale ${Math.round(ageMs/1000)}s)`);
  else setIndicator("bad", `agent offline (${Math.round(ageMs/60000)}m ago)`);
}

function renderStatusCard() {
  if (!lastStatus) return;
  $("#status-mode").textContent = lastStatus.mode || "—";
  $("#status-allow").textContent =
    (lastStatus.allow && lastStatus.allow.length) ? lastStatus.allow.join(", ") : "(none)";
  $("#status-until").textContent = lastStatus.until ? formatUntil(lastStatus.until) : "—";
  $("#status-host").textContent = lastStatus.host || "—";
  $("#status-seen").textContent = `${formatRelative(lastStatusAt)}`;
}

function formatUntil(iso) {
  try {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString();
  } catch { return iso; }
}

function formatRelative(ts) {
  const sec = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  return `${Math.round(sec / 3600)}h ago`;
}

function ingestStatus(raw) {
  let parsed;
  try { parsed = JSON.parse(raw); } catch { return; }
  lastStatus = parsed;
  lastStatusAt = Date.now();
  renderStatusCard();
  classifyHeartbeat();
}

function startStatusStream() {
  if (evtSource) {
    evtSource.close();
    evtSource = null;
  }
  if (!config?.status_topic) return;

  // Backfill the most recent message immediately.
  fetch(statusPollUrl())
    .then((r) => r.text())
    .then((text) => {
      let lastMsg = null;
      for (const line of text.split("\n")) {
        if (!line.trim()) continue;
        try {
          const evt = JSON.parse(line);
          if (evt.event === "message") lastMsg = evt;
        } catch { /* ignore */ }
      }
      if (lastMsg) {
        lastStatusAt = (lastMsg.time || Math.round(Date.now() / 1000)) * 1000;
        try {
          lastStatus = JSON.parse(lastMsg.message);
        } catch { lastStatus = null; }
        renderStatusCard();
        classifyHeartbeat();
      }
    })
    .catch(() => { /* ignore */ });

  evtSource = new EventSource(statusSseUrl());
  evtSource.addEventListener("message", (e) => {
    // ntfy /sse delivers raw message bodies in `e.data`.
    ingestStatus(e.data);
  });
  evtSource.onerror = () => {
    classifyHeartbeat();
    // EventSource auto-reconnects.
  };
}

// ---------- UI wiring ---------------------------------------------------

function renderCategories() {
  const host = $("#categories");
  host.innerHTML = "";
  for (const cat of DEFAULT_CATEGORIES) {
    const id = `cat-${cat}`;
    const wrap = document.createElement("label");
    wrap.className = "check";
    wrap.innerHTML = `<input type="checkbox" id="${id}" value="${cat}"> ${cat}`;
    host.appendChild(wrap);
  }
}

function getAllowSelection() {
  return $$("#categories input:checked").map((el) => el.value);
}

function feedback(msg, kind = "info") {
  const el = $("#action-feedback");
  el.textContent = msg;
  el.className = `feedback ${kind}`;
  if (kind !== "error") {
    setTimeout(() => {
      if (el.textContent === msg) el.textContent = "";
    }, 4000);
  }
}

async function sendCmd(builder, label) {
  if (!config) return feedback("set up config first", "error");
  try {
    const cmd = await builder();
    await publish(cmd);
    feedback(`sent: ${label}`, "ok");
  } catch (exc) {
    console.error(exc);
    feedback(`failed: ${exc.message || exc}`, "error");
  }
}

function attachActions() {
  $("#btn-block").addEventListener("click", () => {
    sendCmd(
      async () => ({ cmd: "block", nonce: Date.now() }),
      "block everything",
    );
  });

  $$(".quick").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mins = parseInt(btn.dataset.mins, 10);
      const allow = getAllowSelection();
      const allowList = allow.length ? allow : null; // null => all categories
      const until = mins > 0
        ? new Date(Date.now() + mins * 60_000).toISOString().replace(/\.\d+Z$/, "Z")
        : null;
      const human = (allowList ? `unblock ${allowList.join(",")}` : "unblock all")
        + (until ? ` for ${mins}m` : " indefinitely");
      sendCmd(
        async () => {
          const c = { cmd: "unblock", nonce: Date.now() };
          if (allowList) c.allow = allowList;
          if (until) c.until = until;
          return c;
        },
        human,
      );
    });
  });

  $("#btn-unblock-some").addEventListener("click", () => {
    const allow = getAllowSelection();
    if (!allow.length) return feedback("pick at least one category", "error");
    sendCmd(
      async () => ({ cmd: "unblock", nonce: Date.now(), allow }),
      `allowlist ${allow.join(",")}`,
    );
  });

  $("#btn-ping").addEventListener("click", () => {
    sendCmd(
      async () => ({ cmd: "ping", nonce: Date.now() }),
      "ping",
    );
  });

  $("#btn-settings").addEventListener("click", (e) => {
    e.preventDefault();
    showSetup();
  });
}

function attachSetup() {
  $("#setup-save").addEventListener("click", async () => {
    const raw = $("#setup-json").value.trim();
    let parsed;
    try { parsed = JSON.parse(raw); }
    catch (e) { return setupError("not valid JSON: " + e.message); }
    for (const k of ["cmd_topic", "secret"]) {
      if (!parsed[k]) return setupError(`missing field: ${k}`);
    }
    parsed.ntfy_base = parsed.ntfy_base || "https://ntfy.sh";
    try { await importSecret(parsed.secret); }
    catch (e) { return setupError("bad secret: " + e.message); }
    saveConfig(parsed);
    setupError("");
    await activate(parsed);
  });

  $("#setup-clear").addEventListener("click", () => {
    clearConfig();
    location.reload();
  });
}

function setupError(msg) {
  $("#setup-error").textContent = msg;
}

function showSetup() {
  $("#setup-section").classList.remove("hidden");
  $("#actions-card").classList.add("hidden");
  $("#status-card").classList.add("hidden");
  if (config) $("#setup-json").value = JSON.stringify(config, null, 2);
}

function showRunning() {
  $("#setup-section").classList.add("hidden");
  $("#actions-card").classList.remove("hidden");
  $("#status-card").classList.remove("hidden");
}

async function activate(cfg) {
  config = cfg;
  secretKey = await importSecret(cfg.secret);
  showRunning();
  startStatusStream();
}

// Re-classify the indicator once a second so it ages correctly.
setInterval(() => {
  if (lastStatus) {
    $("#status-seen").textContent = formatRelative(lastStatusAt);
    classifyHeartbeat();
  }
}, 1000);

// ---------- boot --------------------------------------------------------

async function boot() {
  renderCategories();
  attachActions();
  attachSetup();
  const stored = loadConfig();
  if (!stored) {
    showSetup();
    return;
  }
  try {
    await activate(stored);
  } catch (e) {
    setupError("stored config invalid: " + e.message);
    showSetup();
  }
}

boot();

// Optional service worker for installability/offline shell.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => { /* ignore */ });
}
