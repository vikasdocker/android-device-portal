/* Android Device Portal - front end (no build step) */
const $ = (id) => document.getElementById(id);
let STATE = null;
let SETTINGS_DIRTY = false;
let CARDS = {};           // device_id -> {el, refs}
let RUNS = [];
let HEALTH_TS = 0;

const STATUS_LABEL = {
  idle: "idle", queued: "queued", running: "RUNNING",
  pass: "PASS", fail: "FAIL", error: "ERROR", stopped: "stopped", skipped: "skipped",
};

async function api(path, opts) {
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let msg = resp.statusText;
    try {
      const text = await resp.text();
      try {
        const data = JSON.parse(text);
        msg = (data && data.detail) || text || msg;
      } catch (e) {
        if (text) msg = text.slice(0, 400);
      }
    } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  return resp.json();
}

function notify(msg, isError) {
  const el = $("settings-hint");
  el.textContent = msg;
  el.style.color = isError ? "var(--red)" : "var(--green)";
  if (isError) console.error(msg);
  showToast(msg, isError);
}

/* Big visible toast - errors must never hide in the small settings hint line. */
let TOAST_TIMER = 0;
function showToast(msg, isError) {
  let el = $("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(TOAST_TIMER);
  TOAST_TIMER = setTimeout(() => { el.className = "toast"; }, isError ? 9000 : 4000);
}

/* ------------------------------------------------------------------ state */
async function loadState() {
  try {
    STATE = await api("/api/state");
    render();
  } catch (err) {
    $("agent-dot").className = "dot bad";
    $("agent-text").textContent = "portal unreachable: " + err.message;
  }
  if (Date.now() - HEALTH_TS > 30000) loadHealth();
}

async function loadHealth() {
  HEALTH_TS = Date.now();
  try {
    const h = await api("/api/health");
    if (h.agent && h.agent.ok) {
      $("agent-dot").className = "dot ok";
      const ram = h.agent.free_ram_mb ? ` · ${h.agent.free_ram_mb} MB RAM free` : "";
      const avds = (h.agent.avds || []).length;
      $("agent-text").textContent = `agent online · ${avds} AVD(s)${ram}`;
    } else {
      $("agent-dot").className = "dot bad";
      $("agent-text").textContent = "agent offline — run host-agent\\run-agent.bat";
    }
  } catch (err) {
    $("agent-dot").className = "dot bad";
    $("agent-text").textContent = "agent check failed";
  }
}

async function loadRuns() {
  try {
    const data = await api("/api/runs?limit=50");
    RUNS = data.runs || [];
    renderRuns();
  } catch (err) { /* ignore */ }
}

/* --------------------------------------------------------------- settings */
function fillSettings(s) {
  if (SETTINGS_DIRTY) return;
  $("s-package").value = s.package_name || "";
  $("s-mode").value = s.install_mode || "apk";
  $("s-apk").value = s.apk_path || "";
  $("s-aab").value = s.aab_path || "";
  $("s-play").value = s.play_url || "";
  $("s-settle").value = s.settle_seconds ?? 8;
  $("s-shots").value = s.screenshot_offsets || "2,8,20";
  $("s-quickboot").checked = !!s.quick_boot;
  $("s-wipe").checked = !!s.wipe_before_run;
  $("s-autostop").checked = s.auto_stop_emulator !== false;
}

function collectSettings() {
  return {
    package_name: $("s-package").value.trim(),
    install_mode: $("s-mode").value,
    apk_path: $("s-apk").value.trim(),
    aab_path: $("s-aab").value.trim(),
    play_url: $("s-play").value.trim(),
    settle_seconds: Number($("s-settle").value || 8),
    screenshot_offsets: $("s-shots").value.trim() || "2,8,20",
    quick_boot: $("s-quickboot").checked,
    wipe_before_run: $("s-wipe").checked,
    auto_stop_emulator: $("s-autostop").checked,
  };
}

async function saveSettings() {
  const settings = collectSettings();
  await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  SETTINGS_DIRTY = false;
  notify("settings saved");
  return settings;
}

/* ------------------------------------------------------------------ render */
function render() {
  if (!STATE) return;
  const q = STATE.queue || {};
  fillSettings(STATE.settings || {});

  const deviceCount = (STATE.devices || []).length;
  const countEl = $("device-count");
  if (countEl) {
    countEl.textContent = deviceCount
      ? `${deviceCount}-device Play Store test bench`
      : "device matrix";
  }

  $("btn-run-all").disabled = !!q.active;
  $("btn-stop").disabled = !q.active;
  $("btn-skip").disabled = !q.active;

  const sum = $("summary");
  if (q.active) {
    sum.innerHTML = `⏳ <b>${q.index + 1}/${q.total}</b> · ✅ ${q.passed} · ❌ ${q.failed} · ${escapeHtml(q.message || "")}`;
  } else {
    const last = RUNS.length ? RUNS[0] : null;
    sum.textContent = last
      ? `idle · last run: ${last.device_id} → ${STATUS_LABEL[last.status] || last.status}`
      : "idle · nothing has run yet";
  }

  const grid = $("device-grid");
  for (const dev of STATE.devices || []) {
    let card = CARDS[dev.id];
    if (!card) {
      const el = document.createElement("div");
      el.className = "card device";
      el.innerHTML = `
        <div class="device-head">
          <div>
            <div class="device-name">${escapeHtml(dev.name)} <span class="muted">${escapeHtml(dev.size || "")}</span></div>
            <div class="chips">
              <span class="chip">API ${escapeHtml(String(dev.api))}</span>
              <span class="chip">Android ${escapeHtml(String(dev.android))}</span>
              ${dev.play_store ? '<span class="chip play">Play Store</span>' : '<span class="chip">no Play Store</span>'}
              ${(dev.tags || []).map((t) => `<span class="chip">${escapeHtml(t)}</span>`).join("")}
            </div>
          </div>
          <span class="status-chip idle">idle</span>
        </div>
        <div class="thumb" hidden><img alt="live screen"></div>
        <div class="device-step muted"></div>
        <div class="row end">
          <button class="btn small b-wipe" title="Wipe this device's data on next boot">🧹 Wipe</button>
          <button class="btn small b-detail" hidden>Details</button>
          <button class="btn small primary b-run">▶ Run</button>
        </div>`;
      grid.appendChild(el);
      card = {
        el,
        statusEl: el.querySelector(".status-chip"),
        stepEl: el.querySelector(".device-step"),
        thumbEl: el.querySelector(".thumb"),
        imgEl: el.querySelector(".thumb img"),
        detailBtn: el.querySelector(".b-detail"),
      };
      card.runBtn = el.querySelector(".b-run");
      card.runBtn.addEventListener("click", () => runOne(dev.id));
      el.querySelector(".b-wipe").addEventListener("click", () => wipeDevice(dev.id));
      card.detailBtn.addEventListener("click", () => { if (card.lastRun) openRun(card.lastRun); });
      CARDS[dev.id] = card;
    }

    const st = dev.status || "idle";
    card.statusEl.className = "status-chip " + st;
    card.statusEl.textContent = STATUS_LABEL[st] || st;

    if (dev.is_running) {
      const step = STATE.agent && STATE.agent.step ? STATE.agent.step : "starting";
      const logs = (STATE.agent && STATE.agent.log) || [];
      const lastLog = logs.length ? logs[logs.length - 1].msg : "";
      card.stepEl.textContent = `${step} — ${lastLog}`.slice(0, 140);
      card.thumbEl.hidden = false;
      card.imgEl.src = `/api/devices/${dev.id}/live.png?t=${Date.now()}`;
    } else {
      card.stepEl.textContent = "";
      card.thumbEl.hidden = true;
    }

    card.runBtn.disabled = !!q.active;
    card.lastRun = dev.last_run_id || null;
    card.detailBtn.hidden = !card.lastRun;
  }
}

function renderRuns() {
  const tbody = $("runs-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const r of RUNS) {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    const st = STATUS_LABEL[r.status] || r.status;
    tr.innerHTML = `
      <td>${escapeHtml((r.started_at || "").replace("T", " "))}</td>
      <td>${escapeHtml(r.device_id)}</td>
      <td><span class="status-chip ${escapeHtml(r.status)}">${escapeHtml(st)}</span></td>
      <td class="muted">view →</td>`;
    tr.addEventListener("click", () => openRun(r.id));
    tbody.appendChild(tr);
  }
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ----------------------------------------------------------------- actions */
async function runAll() {
  try {
    const settings = collectSettings();
    await api("/api/queue/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings }),
    });
    SETTINGS_DIRTY = false;
    notify("queue started");
    loadState();
  } catch (err) {
    $("settings-panel").open = true;
    notify("could not start: " + err.message, true);
  }
}

async function runOne(deviceId) {
  try {
    const settings = collectSettings();
    await api("/api/queue/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings, device_ids: [deviceId] }),
    });
    notify("started " + deviceId);
    loadState();
  } catch (err) {
    $("settings-panel").open = true;
    notify("could not start: " + err.message, true);
  }
}

async function stopAll() {
  try { await api("/api/queue/stop", { method: "POST" }); notify("stopping…"); }
  catch (err) { notify(err.message, true); }
}

async function skipCurrent() {
  try { await api("/api/queue/skip", { method: "POST" }); notify("skipping current device…"); }
  catch (err) { notify(err.message, true); }
}

async function killEmulator() {
  if (!confirm("Force-kill the emulator using port 5554?")) return;
  try { await api("/api/agent/kill-emulator", { method: "POST" }); notify("kill signal sent"); }
  catch (err) { notify(err.message, true); }
}

async function wipeDevice(deviceId) {
  if (!confirm(`Wipe ${deviceId} device data on its next boot?`)) return;
  try { await api(`/api/devices/${deviceId}/wipe`, { method: "POST" }); notify(`${deviceId} will be wiped on next run`); }
  catch (err) { notify(err.message, true); }
}

/* ------------------------------------------------------------------- modal */
async function openRun(runId) {
  const modal = $("modal");
  $("modal-title").textContent = "Run " + runId;
  $("modal-body").innerHTML = "<p class='muted'>loading…</p>";
  modal.hidden = false;
  try {
    const run = await api("/api/runs/" + encodeURIComponent(runId));
    const res = run.result || {};
    const shots = (run.artifacts || []).filter((a) => a.path.startsWith("screenshots/"));
    const files = (run.artifacts || []).filter((a) => !a.path.startsWith("screenshots/"));
    const info = res.device_info || {};
    $("modal-body").innerHTML = `
      <div class="row" style="gap:16px;margin:8px 0">
        <span class="status-chip ${escapeHtml(run.status)}">${escapeHtml(STATUS_LABEL[run.status] || run.status)}</span>
        <span class="muted">${escapeHtml(run.started_at || "")} → ${escapeHtml(run.finished_at || "")}</span>
      </div>
      ${(res.reasons || []).length ? `<p>${res.reasons.map((r) => "• " + escapeHtml(r)).join("<br>")}</p>` : ""}
      ${(res.crashes || []).length ? `<details open><summary>Crash / ANR lines (${res.crashes.length})</summary><pre style="color:var(--red)">${escapeHtml(res.crashes.join("\n"))}</pre></details>` : ""}
      <div class="shot-grid">
        ${shots.map((s) => `<a href="/api/runs/${encodeURIComponent(run.id)}/file?path=${encodeURIComponent(s.path)}" target="_blank">
            <img src="/api/runs/${encodeURIComponent(run.id)}/file?path=${encodeURIComponent(s.path)}" alt="${escapeHtml(s.path)}"></a>`).join("") || "<p class='muted'>no screenshots</p>"}
      </div>
      <details><summary>Device info</summary>
        <div class="kv">${Object.entries(info).map(([k, v]) => `<div>${escapeHtml(k)}</div><div>${escapeHtml(v)}</div>`).join("")}</div>
        ${res.meminfo_tail ? `<pre>${escapeHtml(res.meminfo_tail)}</pre>` : ""}
      </details>
      <details><summary>Files</summary>
        ${files.map((f) => `<div><a href="/api/runs/${encodeURIComponent(run.id)}/file?path=${encodeURIComponent(f.path)}" target="_blank">${escapeHtml(f.path)}</a> <span class="muted">(${Math.round(f.size / 1024)} KB)</span></div>`).join("") || "<span class='muted'>none</span>"}
      </details>
      <details><summary>Result JSON</summary><pre>${escapeHtml(JSON.stringify(res, null, 2))}</pre></details>
    `;
  } catch (err) {
    $("modal-body").innerHTML = `<p style="color:var(--red)">${escapeHtml(err.message)}</p>`;
  }
}

/* ------------------------------------------------------------------ uploads */
async function upload(kind, file) {
  if (!file) return;
  notify(`uploading ${file.name}…`);
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await api(`/api/upload?kind=${kind}`, { method: "POST", body: fd });
    if (res.host_path) {
      if (kind === "apk") $("s-apk").value = res.host_path;
      else $("s-aab").value = res.host_path;
      notify(`${file.name} uploaded (${Math.round(res.size / 1048576)} MB)`);
    } else {
      notify("uploaded, but HOST_DATA_DIR is not configured — type the path manually", true);
    }
  } catch (err) {
    notify("upload failed: " + err.message, true);
  }
}

/* ------------------------------------------------------------------- wiring */
for (const id of ["s-package", "s-mode", "s-apk", "s-aab", "s-play", "s-settle", "s-shots", "s-quickboot", "s-wipe", "s-autostop"]) {
  $(id).addEventListener("input", () => { SETTINGS_DIRTY = true; });
}

$("btn-run-all").addEventListener("click", runAll);
$("btn-stop").addEventListener("click", stopAll);
$("btn-skip").addEventListener("click", skipCurrent);
$("btn-kill").addEventListener("click", killEmulator);
$("btn-save").addEventListener("click", () => saveSettings().catch((e) => notify(e.message, true)));
$("btn-up-apk").addEventListener("click", () => $("f-apk").click());
$("btn-up-aab").addEventListener("click", () => $("f-aab").click());
$("f-apk").addEventListener("change", (e) => upload("apk", e.target.files[0]));
$("f-aab").addEventListener("change", (e) => upload("aab", e.target.files[0]));
$("modal-close").addEventListener("click", () => { $("modal").hidden = true; });
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) $("modal").hidden = true; });

loadState();
loadRuns();
setInterval(loadState, 2500);
setInterval(loadRuns, 10000);
