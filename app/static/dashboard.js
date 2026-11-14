// The dashboard: asks GET /state every 2 seconds and redraws what changed.
// Add ?sample=1 to the address to show a saved example instead of live data.
// This file draws the top bar and the procurement decision and handles every click;
// the other panels are drawn by os_panels.js, os_camera.js and os_inventory.js.

const SAMPLE = new URLSearchParams(location.search).has("sample");
const AUTOPILOT_LIMIT_KEY = "stockpot.autopilot_limit";  // the spend limit to restore when leaving Safe mode
let state = null;       // the latest answer from GET /state
let selected = null;    // id of the request shown in the decision panel
let selectedProductKey = null;  // the product clicked in the camera picture; its tile, reading and inventory row are highlighted
let newest = null;      // id of the newest request seen, so a new one is shown as soon as it arrives
let trouble = "";       // a problem to show in the top bar (lost connection, a refused mode change)
const folds = new Map();  // which fold-out sections of the decision the reader opened or closed

async function send(method, path, body) {
  const response = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const answer = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof answer.detail === "string" ? answer.detail : "The request was not accepted.");
  return answer;
}

function drawHeader() {
  const health = state.health || {};
  const plan = state.requests.map((request) => request.decision.chosen_plan).find(Boolean);
  const cutoffs = plan ? plan.lines.map((line) => line.offer.order_cutoff).filter(Boolean).sort() : [];
  const seen = state.shelf.filter((item) => item.confidence != null);
  const confidence = seen.reduce((sum, item) => sum + item.confidence, 0) / seen.length;
  const fact = (label, value) => `<span>${label} <b>${esc(value)}</b></span>`;
  put("telemetry", fact("Now", dayTime(state.now)) + fact("Next delivery", plan ? day(plan.delivery_date) : "—")
    + fact("Order cutoff", cutoffs.length ? dayTime(cutoffs[0]) : "—")
    + fact("Cameras", seen.length ? `${percent(confidence)} avg confidence` : "waiting for a reading"));
  // Only problems are shown here; what is working is listed under "Data feeding the model".
  const problems = [
    trouble, state.standing_limits ? "" : "Supplier not connected",
    health.gemini_key_set ? "" : "No GEMINI_API_KEY", health.exa_key_set ? "" : "No EXA_API_KEY",
  ];
  put("chips", problems.filter(Boolean).map((text) => `<span class="chip bad">${esc(text)}</span>`).join(""));
}

// Safe mode and Autopilot are two settings of one rule: the auto-approve spend limit.
// Safe mode sets it to zero, so every order waits for a person. Autopilot puts the limit back.
// Autopilot still only auto-orders exact, full, on-time plans under the limit with clear web checks.
function drawMode() {
  const limit = Number(state.policy.auto_approve_max_total_eur);
  const button = (mode, on, label, hint) =>
    `<button data-mode="${mode}" class="${on ? "on" : ""}" title="${hint}" ${SAMPLE ? "disabled" : ""}>${label}</button>`;
  put("mode", button("safe", !limit, "Safe mode", "Every order waits for a person's approval")
    + button("autopilot", limit, "Autopilot",
      "Orders alone only when exact product, full qty, on time, under the limit, and web checks are clear"));
  put("mode-badge", limit
    ? pill("autopilot", `Autopilot · exact matches up to ${euros(limit)}`)
    : pill("safe", "Safe mode · every order needs approval"));
}

async function setMode(mode) {
  const limit = Number(state.policy.auto_approve_max_total_eur);
  trouble = "";
  if ((mode === "safe") === !limit) return;  // already in that mode
  if (mode === "safe") localStorage.setItem(AUTOPILOT_LIMIT_KEY, String(limit));
  const restored = Number(localStorage.getItem(AUTOPILOT_LIMIT_KEY)) || 500;
  try {
    await send("PUT", "/policy", { ...state.policy, auto_approve_max_total_eur: mode === "safe" ? 0 : restored });
  } catch (error) { trouble = `Mode not changed: ${error.message}`; }
  poll();
}

function drawDecision() {
  const request = state.requests.find((item) => item.id === selected);
  if (!request) {
    put("decision", `<p class="small">Nothing yet. Take cartons off the shelf, or type a request above.</p>`);
    return;
  }
  let actions = "";
  if (request.decision.status === "awaiting_approval") {
    const total = request.decision.chosen_plan ? " " + money(request.decision.chosen_plan.total) : "";
    actions = `
      <div class="approval">
        ${request.approval ? request.approval.qr_svg : ""}
        <div class="approval-body">
          <b>Needs approval</b>
          <input type="text" id="note" placeholder="Note (optional)" maxlength="300">
          <div class="row"><button class="reject" data-action="reject">Reject</button>
            <button class="approve" data-action="approve">Approve${total}</button></div>
          <span id="approval-error" class="error"></span>
        </div>
      </div>`;
  } else if (request.decision.status === "needs_clarification" && request.source === "typed") {
    actions = `
      <form id="answer-form" class="ask">
        <input type="text" id="answer" placeholder="Your answer" maxlength="300" autocomplete="off">
        <button type="submit" class="primary">Answer</button>
      </form>`;
  }
  put("decision", decisionHtml(request, actions, (key, fallback) => (folds.has(key) ? folds.get(key) : fallback)));
}

function draw() {
  if (state.requests[0] && state.requests[0].id !== newest) newest = selected = state.requests[0].id;
  if (!state.requests.some((request) => request.id === selected)) selected = state.requests[0]?.id || null;
  drawHeader(); drawMode(); drawDecision();
  drawKpis(state); drawReplenishment(state); drawCamera(state);
  drawInventory(state); drawOrders(state); drawActivity(state); drawSources(state);
}

// A click on a product in the camera picture selects it in the other panels. Nothing is ordered from here.
function selectProduct(productKey) {
  selectedProductKey = productKey;
  const showing = state.requests.find((request) => request.id === selected);
  // Show that product's newest request, unless one of its requests is already showing.
  if (productKey && showing?.product_key !== productKey) {
    selected = state.requests.find((request) => request.product_key === productKey)?.id || selected;
  }
  draw();
}

async function poll() {
  try {
    state = await (await fetch(SAMPLE ? "/static/sample_state.json" : "/state")).json();
    if (trouble === "Stockpot is not answering") trouble = "";
  } catch (error) {
    trouble = "Stockpot is not answering";
    if (!state) { put("chips", `<span class="chip bad">${trouble}</span>`); return; }
    // The video comes through Stockpot, so it has stopped as well. Marking the camera as
    // not connected empties the viewport now and reconnects the feed once Stockpot is back.
    state = { ...state, detections: [], health: { ...state.health, camera_connected: false } };
  }
  draw();
}

$("venue").textContent = BASELINE.venue;
$("mode").addEventListener("click", (event) => { if (event.target.dataset.mode) setMode(event.target.dataset.mode); });
$("camera-tabs").addEventListener("click", (event) => {
  if (event.target.dataset.camera) { camera = Number(event.target.dataset.camera); drawCamera(state); }
});
// A click on a product's box selects that product; a click anywhere else in the picture clears the selection.
$("camera-viewport").addEventListener("click", (event) => {
  if (state) selectProduct(event.target.closest(".hit")?.dataset.productKey || null);
});
for (const id of ["inventory-search", "inventory-status"]) $(id).addEventListener("input", () => state && drawInventory(state));

$("request-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("request-text").value.trim();
  if (!text) return;
  try {
    selected = (await send("POST", "/requests", { text })).request_id;
    $("request-text").value = ""; $("request-error").textContent = "";
    poll();
  } catch (error) { $("request-error").textContent = error.message; }
});

$("decision").addEventListener("click", async (event) => {
  // Remember a fold-out the reader opened or closed, so the next redraw keeps it that way.
  // (The click arrives before the browser flips it, hence the "!".)
  const summary = event.target.closest("summary");
  if (summary) folds.set(summary.parentElement.dataset.key, !summary.parentElement.open);
  const button = event.target.closest("[data-action]");
  if (!button) return;
  button.disabled = true;
  try { await send("POST", `/approvals/${selected}`, { action: button.dataset.action, note: $("note").value }); poll(); }
  catch (error) { $("approval-error").textContent = error.message; button.disabled = false; }
});

$("decision").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("answer").value.trim();
  if (text) { selected = (await send("POST", "/requests", { text, reply_to: selected })).request_id; poll(); }
});

// --- Rules panel -----------------------------------------------------------
const form = $("rules-form");
$("open-rules").addEventListener("click", () => {
  const policy = state.policy;
  for (const input of form.elements) {
    if (!input.name || input.name === "category") continue;
    if (input.type === "checkbox") input.checked = policy[input.name]; else input.value = Number(policy[input.name]);
  }
  $("rule-categories").innerHTML = state.category_names.map((name) => `
    <label class="check"><input type="checkbox" name="category" value="${esc(name)}"
      ${policy.allowed_categories.includes(name) ? "checked" : ""}> ${esc(name)}</label>`).join("");
  $("rules-error").textContent = "";
  $("rules").showModal();
});
$("close-rules").addEventListener("click", () => $("rules").close());
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const policy = { allowed_categories: [...form.querySelectorAll("[name=category]:checked")].map((box) => box.value) };
  for (const input of form.elements) {
    if (!input.name || input.name === "category") continue;
    policy[input.name] = input.type === "checkbox" ? input.checked : input.value;
  }
  try { await send("PUT", "/policy", policy); $("rules").close(); poll(); }
  catch (error) { $("rules-error").textContent = error.message; }
});

poll();
setInterval(poll, 2000);
