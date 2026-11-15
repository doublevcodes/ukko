// The dashboard: asks GET /state every second and redraws what changed.
// Add ?sample=1 to the address to show a saved example instead of live data.
// This file draws the top bar and the procurement decision and handles every click;
// the other panels are drawn by os_panels.js, os_camera.js and os_inventory.js.

const SAMPLE = new URLSearchParams(location.search).has("sample");
const AUTOPILOT_LIMIT_KEY = "ukko.autopilot_limit";  // the spend limit to restore when leaving Safe mode
// Same as app/state.py OPEN_STATUSES: in-progress work that can take over the decision panel.
const OPEN = new Set(["working", "needs_clarification", "awaiting_approval"]);
const isOpen = (request) => OPEN.has(request.decision.status);
let state = null;       // the latest answer from GET /state
let selected = null;    // id of the request shown in the decision panel
let selectedProductKey = null;  // the product clicked in the camera picture; its tile, reading and inventory row are highlighted
let orderModalId = null;  // id of the request shown in the Orders details modal
let newest = null;      // id of the newest request seen, so a new one is shown as soon as it arrives
let trouble = "";       // a problem to show in the top bar (lost connection, a refused mode change)
const folds = new Map();  // which fold-out sections of the decision the reader opened or closed
const orderFolds = new Map();  // fold-outs inside the Orders details modal

async function send(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  const answer = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = answer.detail;
    let message = `The request was not accepted (${response.status}).`;
    if (typeof detail === "string") message = detail;
    else if (Array.isArray(detail) && detail[0]?.msg) message = detail.map((item) => item.msg).join(" ");
    throw new Error(message);
  }
  return answer;
}

function drawHeader() {
  const health = state.health || {};
  const payment = state.payment || {};
  const paymentHint = !health.stripe_configured ? "No STRIPE_SECRET_KEY"
    : payment.ready ? "" : "No kitchen card";
  const problems = [
    trouble, state.standing_limits ? "" : "Supplier not connected",
    health.gemini_key_set ? "" : "No GEMINI_API_KEY", health.exa_key_set ? "" : "No EXA_API_KEY",
    paymentHint,
  ];
  put("chips", problems.filter(Boolean).map((text) => `<span class="chip bad">${esc(text)}</span>`).join(""));
}

let embeddedCheckout = null;
let setupSessionId = null;

function drawPayment() {
  const payment = state.payment || {};
  const status = $("payment-status");
  const button = $("payment-setup");
  if (!status || !button) return;
  if (!payment.secret_configured) {
    status.textContent = "Set STRIPE_SECRET_KEY (test mode) in .env, then add a card.";
  } else if (!payment.publishable_configured) {
    status.textContent = "Set STRIPE_PUBLISHABLE_KEY in .env so the card form can load here.";
  } else if (payment.ready) {
    status.textContent = `Kitchen card on file: ${payment.label}. Auto-approve and Approve both charge this card.`;
  } else {
    status.textContent = "No card on file yet. Add one once — later orders charge it automatically.";
  }
  button.disabled = SAMPLE || !payment.secret_configured || !payment.publishable_configured;
  button.textContent = payment.ready ? "Change card" : "Add card (once)";
}

async function destroyEmbeddedCheckout() {
  if (embeddedCheckout) {
    try { await embeddedCheckout.destroy(); } catch (_) { /* already gone */ }
    embeddedCheckout = null;
  }
  setupSessionId = null;
  const mount = $("payment-checkout");
  if (mount) { mount.innerHTML = ""; mount.hidden = true; }
  const cancel = $("payment-cancel");
  if (cancel) cancel.hidden = true;
  $("settings")?.classList.remove("payment-wide");
}

async function finishEmbeddedSetup() {
  const error = $("payment-error");
  if (!setupSessionId) return;
  const sessionId = setupSessionId;
  // Retry a few times: Stripe may still be flipping the session to complete.
  let lastError = null;
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      const answer = await send("POST", "/payments/complete", { session_id: sessionId });
      state.payment = answer.payment;
      await destroyEmbeddedCheckout();
      drawPayment();
      drawHeader();
      trouble = "";
      return;
    } catch (err) {
      lastError = err;
      const msg = String(err.message || "");
      if (!/not completed/i.test(msg) || attempt === 5) break;
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
  }
  error.textContent = lastError?.message || "Card setup failed.";
}

async function mountEmbeddedCheckout() {
  const error = $("payment-error");
  const mount = $("payment-checkout");
  const cancel = $("payment-cancel");
  error.textContent = "";
  if (SAMPLE) return;
  if (typeof Stripe !== "function") {
    error.textContent = "Stripe.js did not load. Check your network and reload.";
    return;
  }
  await destroyEmbeddedCheckout();
  $("payment-setup").disabled = true;
  try {
    const answer = await send("POST", "/payments/setup", {});
    setupSessionId = answer.session_id;
    const stripe = Stripe(answer.publishable_key);
    const create = stripe.createEmbeddedCheckoutPage || stripe.initEmbeddedCheckout;
    if (typeof create !== "function") {
      throw new Error("This Stripe.js build has no embedded Checkout. Reload or check the script URL.");
    }
    embeddedCheckout = await create.call(stripe, {
      fetchClientSecret: async () => answer.client_secret,
      onComplete: () => { finishEmbeddedSetup(); },
    });
    mount.hidden = false;
    cancel.hidden = false;
    $("settings").classList.add("payment-wide");
    embeddedCheckout.mount("#payment-checkout");
    mount.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (err) {
    error.textContent = err.message;
    await destroyEmbeddedCheckout();
  } finally {
    drawPayment();
  }
}

// Safe mode and Autopilot are two settings of one rule: the auto-approve spend limit.
// Safe mode sets it to zero, so every order waits for a person. Autopilot puts the limit back.
// Autopilot auto-orders exact or closest catalogue matches that are full, on-time, under the
// limit, with clear web checks. True substitutes (Plan B shortfalls) still need a person.
function drawMode() {
  const limit = Number(state.policy.auto_approve_max_total_eur);
  const button = (mode, on, label, hint) =>
    `<button data-mode="${mode}" class="${on ? "on" : ""}" title="${hint}" ${SAMPLE ? "disabled" : ""}>${label}</button>`;
  put("mode", button("safe", !limit, "Safe mode", "Every order waits for a person's approval")
    + button("autopilot", limit, "Autopilot",
      "Orders alone when exact or closest match, full qty, on time, under the limit, and web checks are clear"));
  put("mode-hint", limit
    ? `Autopilot is on · exact or closest matches up to ${euros(limit)} auto-approve without a person.`
    : "Safe mode is on · every order needs a person's approval.");
  drawPayment();
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

function approvalActions(request, noteId, errorId) {
  if (request.decision.status !== "awaiting_approval") return "";
  const total = request.decision.chosen_plan ? " " + money(request.decision.chosen_plan.total) : "";
  return `
    <div class="approval-actions">
      <input type="text" id="${noteId}" placeholder="Note (optional)" maxlength="300" autocomplete="off">
      <button type="button" class="reject" data-action="reject">Reject</button>
      <button type="button" class="approve" data-action="approve">Approve${total}</button>
      <span id="${errorId}" class="error"></span>
    </div>`;
}

function drawDecision() {
  const card = document.querySelector(".card.procurement");
  const request = state.requests.find((item) => item.id === selected);
  if (!request) {
    put("decision", "");
    card?.classList.remove("active");
    return;
  }
  card?.classList.add("active");
  const clarifying = request.decision.status === "needs_clarification" && request.source === "typed";
  const actions = clarifying ? "" : approvalActions(request, "note", "approval-error");
  const foldState = (key, fallback) => (folds.has(key) ? folds.get(key) : fallback);
  const composer = clarifying ? `
    <form id="answer-form" class="ask decision-composer">
      <input type="text" id="answer" placeholder="Your answer" maxlength="300" autocomplete="off">
      <button type="submit" class="primary">Answer</button>
    </form>` : "";
  // While the plan is still forming, keep a skeleton up so half-drawn folds never flash.
  if (decisionPending(request)) {
    put("decision", `
      <div class="decision-scroll">${decisionSkeletonHtml(request)}</div>
      <div class="decision-footer">${decisionFooterSkeletonHtml()}</div>`);
    return;
  }
  // Approve strip stays pinned under the scrollable details.
  const body = decisionHtml(request, "", foldState);
  const showFooter = !clarifying && (request.decision.chosen_plan || request.decision.order_confirmation || actions);
  put("decision", `
    <div class="decision-scroll">${body}</div>
    ${showFooter ? `<div class="decision-footer">${summaryHtml(request.decision, actions)}</div>` : ""}
    ${composer}`);
}

function drawOrderDetail() {
  const dialog = $("order-detail");
  if (!dialog?.open || !orderModalId || !state) return;
  const request = state.requests.find((item) => item.id === orderModalId);
  if (!request) {
    orderModalId = null;
    dialog.close();
    put("order-detail-body", "");
    return;
  }
  put("order-detail-body", decisionHtml(
    request,
    approvalActions(request, "order-note", "order-approval-error"),
    (key, fallback) => (orderFolds.has(key) ? orderFolds.get(key) : fallback),
  ));
}

function openOrderDetail(requestId) {
  if (!state || !requestId) return;
  const request = state.requests.find((item) => item.id === requestId);
  if (!request) return;
  orderModalId = requestId;
  orderFolds.clear();
  $("order-detail").showModal();
  drawOrderDetail();
}

function draw() {
  // Auto-show a brand-new open request. Keep a finished one (failed / ordered /
  // rejected) on screen so the outcome is visible — only Approve/Reject clears it.
  if (state.requests[0] && state.requests[0].id !== newest) {
    newest = state.requests[0].id;
    if (isOpen(state.requests[0])) selected = newest;
  }
  const showing = state.requests.find((request) => request.id === selected);
  if (!showing) selected = state.requests.find(isOpen)?.id || null;
  drawHeader(); drawMode(); drawDecision();
  drawReplenishment(state); drawCamera(state);
  drawInventory(state); drawOrders(state); drawActivity(state);
  drawOrderDetail();
}

// A click on a product in the camera picture selects it in the other panels. Nothing is ordered from here.
function selectProduct(productKey) {
  selectedProductKey = productKey;
  const showing = state.requests.find((request) => request.id === selected);
  // Show that product's newest open request, unless one of its requests is already showing.
  if (productKey && showing?.product_key !== productKey) {
    selected = state.requests.find((request) => request.product_key === productKey && isOpen(request))?.id || selected;
  }
  draw();
  if (!productKey) return;
  const row = $("inventory-body")?.querySelector(`tr[data-product-key="${CSS.escape(productKey)}"]`);
  row?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function poll() {
  try {
    state = await (await fetch(SAMPLE ? "/static/sample_state.json" : "/state")).json();
    if (trouble === "Ukko is not answering") trouble = "";
  } catch (error) {
    trouble = "Ukko is not answering";
    if (!state) { put("chips", `<span class="chip bad">${trouble}</span>`); return; }
    // The video comes through Ukko, so it has stopped as well. Marking the camera as
    // not connected empties the viewport now and reconnects the feed once Ukko is back.
    state = { ...state, detections: [], health: { ...state.health, camera_connected: false } };
  }
  draw();
}

function showPage(page) {
  for (const button of $("nav").querySelectorAll("[data-page]")) {
    button.classList.toggle("on", button.dataset.page === page);
  }
  for (const panel of document.querySelectorAll("[data-page-panel]")) {
    panel.hidden = panel.dataset.pagePanel !== page;
  }
}
$("nav").addEventListener("click", (event) => {
  const page = event.target.dataset.page;
  if (page) showPage(page);
});
showPage("operations");

$("mode").addEventListener("click", (event) => { if (event.target.dataset.mode) setMode(event.target.dataset.mode); });
$("camera-tabs").addEventListener("click", (event) => {
  if (event.target.dataset.camera) { camera = Number(event.target.dataset.camera); drawCamera(state); }
});
// A click on a product's box selects that product; a click anywhere else in the picture clears the selection.
$("camera-viewport").addEventListener("click", (event) => {
  if (state) selectProduct(event.target.closest(".hit")?.dataset.productKey || null);
});

$("reset-orders").addEventListener("click", async () => {
  if (SAMPLE) return;
  const confirmed = window.confirm(
    "Clear all orders, requests and on-order stock, and start the shelf from scratch?\n\nThe kitchen card is kept.",
  );
  if (!confirmed) return;
  const button = $("reset-orders");
  const status = $("reset-orders-status");
  button.disabled = true;
  status.textContent = "Resetting…";
  try {
    await send("POST", "/reset", {});
    selected = null;
    selectedProductKey = null;
    newest = null;
    orderModalId = null;
    folds.clear();
    orderFolds.clear();
    status.textContent = "Orders cleared.";
    await poll();
  } catch (error) {
    status.textContent = error.message || "Reset failed.";
  } finally {
    button.disabled = false;
    setTimeout(() => { if (status.textContent === "Orders cleared.") status.textContent = ""; }, 2500);
  }
});
for (const id of ["inventory-search", "inventory-status"]) $(id).addEventListener("input", () => state && drawInventory(state));

// Inventory reorder settings: save on blur or Enter; do not let a click steal the product selection.
$("inventory-body").addEventListener("click", (event) => {
  if (event.target.closest("input.inv-num")) event.stopPropagation();
});
$("inventory-body").addEventListener("change", (event) => {
  const input = event.target.closest("input.inv-num");
  if (input) saveShelfReorder(input);
});
$("inventory-body").addEventListener("keydown", (event) => {
  const input = event.target.closest("input.inv-num");
  if (!input || event.key !== "Enter") return;
  event.preventDefault();
  input.blur();
});

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

async function actOnApproval(requestId, action, note) {
  await send("POST", `/approvals/${requestId}`, { action, note });
  if (selected === requestId) selected = null;  // ordered/rejected stay in Orders, not under Ask Ukko
  poll();
}

$("decision").addEventListener("click", async (event) => {
  // Remember a fold-out the reader opened or closed, so the next redraw keeps it that way.
  // (The click arrives before the browser flips it, hence the "!".)
  const summary = event.target.closest("summary");
  if (summary) folds.set(summary.parentElement.dataset.key, !summary.parentElement.open);
  const button = event.target.closest("[data-action]");
  if (!button) return;
  button.disabled = true;
  try {
    await actOnApproval(selected, button.dataset.action, $("note").value);
  } catch (error) { $("approval-error").textContent = error.message; button.disabled = false; }
});

$("orders").addEventListener("click", (event) => {
  const row = event.target.closest("[data-request-id]");
  if (row) openOrderDetail(row.dataset.requestId);
});

$("close-order-detail").addEventListener("click", () => {
  orderModalId = null;
  $("order-detail").close();
});

$("order-detail").addEventListener("close", () => {
  orderModalId = null;
  put("order-detail-body", "");
});

$("order-detail").addEventListener("click", async (event) => {
  const summary = event.target.closest("summary");
  if (summary) orderFolds.set(summary.parentElement.dataset.key, !summary.parentElement.open);
  const button = event.target.closest("[data-action]");
  if (!button || !orderModalId) return;
  button.disabled = true;
  try {
    await actOnApproval(orderModalId, button.dataset.action, $("order-note")?.value || "");
  } catch (error) {
    const err = $("order-approval-error");
    if (err) err.textContent = error.message;
    button.disabled = false;
  }
});

$("decision").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("answer").value.trim();
  if (text) { selected = (await send("POST", "/requests", { text, reply_to: selected })).request_id; poll(); }
});

// --- Settings (Autopilot + Rules) ------------------------------------------
const form = $("rules-form");
let selectedCategories = [];
function showSettingsTab(tab) {
  for (const button of $("settings-tabs").querySelectorAll("[data-settings-tab]")) {
    button.classList.toggle("on", button.dataset.settingsTab === tab);
  }
  for (const pane of $("settings").querySelectorAll("[data-pane]")) {
    pane.hidden = pane.dataset.pane !== tab;
  }
}
function renderCategoryPicker() {
  const pills = $("rule-category-pills");
  const add = $("rule-category-add");
  if (!pills || !add) return;
  const chosen = new Set(selectedCategories);
  pills.innerHTML = selectedCategories.map((name) => `
    <span class="category-pill">${esc(name)}
      <button type="button" data-remove-category="${esc(name)}" aria-label="Remove ${esc(name)}">×</button>
    </span>`).join("");
  const available = (state?.category_names || []).filter((name) => !chosen.has(name));
  add.innerHTML = `<option value="">Add a product group…</option>` +
    available.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join("");
  add.disabled = available.length === 0;
}
function fillRulesForm() {
  const policy = state.policy;
  for (const input of form.elements) {
    if (!input.name) continue;
    if (input.type === "checkbox") input.checked = Boolean(policy[input.name]);
    else input.value = Number(policy[input.name]);
  }
  selectedCategories = [...policy.allowed_categories];
  renderCategoryPicker();
  $("rules-error").textContent = "";
}
$("rule-category-add")?.addEventListener("change", (event) => {
  const name = event.target.value;
  if (!name || selectedCategories.includes(name)) return;
  selectedCategories = [...selectedCategories, name];
  renderCategoryPicker();
});
$("rule-category-pills")?.addEventListener("click", (event) => {
  const name = event.target.dataset?.removeCategory;
  if (!name) return;
  selectedCategories = selectedCategories.filter((item) => item !== name);
  renderCategoryPicker();
});
$("open-settings").addEventListener("click", () => {
  if (!state) return;
  fillRulesForm();
  showSettingsTab("mode");
  $("settings").showModal();
});
$("close-settings").addEventListener("click", async () => {
  await destroyEmbeddedCheckout();
  $("settings").close();
});
function fillReorderModelPanel() {
  const model = state?.reorder_model;
  const panel = $("reorder-model-panel");
  if (!panel || !model) return;
  const rows = [
    ["model_day_seconds", model.model_day_seconds, "Demo clock: real seconds per model day (1800 = 30 min; 86400 = real time)."],
    ["alpha", model.alpha, "How fast the usage estimate adapts (~last 3–5 model days)."],
    ["sigma_factor", model.sigma_factor, "Day-to-day variability vs Poisson √d (assumption until measured)."],
    ["z critical / standard / perishable", `${model.z?.critical} / ${model.z?.standard} / ${model.z?.perishable}`, "Service level: chance of not stocking out before delivery."],
    ["default_cover_days", model.default_cover_days, "Days of stock to hold after a delivery arrives."],
    ["default_lead_days", model.default_lead_days, "Days until delivery when no Valio quote is available yet."],
    ["multiplier caps", `${model.multiplier_min}–${model.multiplier_max}`, "How far web demand signals may move daily demand."],
    ["extra_lead_days cap", `${model.extra_lead_days_min}–${model.extra_lead_days_max}`, "How far web signals may lengthen lead time."],
  ];
  panel.innerHTML = rows.map(([name, value, reason]) => `
    <div class="reorder-param"><b>${esc(String(name))}</b>: ${esc(String(value))}
      <div class="small">${esc(reason)}</div></div>`).join("");
}
$("settings-tabs").addEventListener("click", async (event) => {
  const tab = event.target.dataset.settingsTab;
  if (!tab) return;
  if (tab !== "payment") await destroyEmbeddedCheckout();
  if (tab === "rules" && state) fillRulesForm();
  if (tab === "payment" && state) drawPayment();
  if (tab === "reorder" && state) fillReorderModelPanel();
  if (tab === "trust") {
    $("trust-test-result").textContent = "";
    $("trust-test-error").textContent = "";
  }
  showSettingsTab(tab);
});
$("settings-trust")?.addEventListener("click", async (event) => {
  const scenario = event.target.dataset.trustScenario;
  if (!scenario) return;
  $("trust-test-error").textContent = "";
  $("trust-test-result").textContent = "Running…";
  try {
    const result = await send("POST", "/trust-tests", { scenario });
    $("trust-test-result").textContent = JSON.stringify(result, null, 2);
    poll();
  } catch (error) {
    $("trust-test-result").textContent = "";
    $("trust-test-error").textContent = error.message;
  }
});
$("payment-setup")?.addEventListener("click", () => { mountEmbeddedCheckout(); });
$("payment-cancel")?.addEventListener("click", async () => {
  $("payment-error").textContent = "";
  await destroyEmbeddedCheckout();
  drawPayment();
});
const paymentFlag = new URLSearchParams(location.search).get("payment");
if (paymentFlag === "ready") trouble = "";
else if (paymentFlag === "failed") trouble = "Card setup failed";
else if (paymentFlag === "cancelled") trouble = "Card setup cancelled";
else if (paymentFlag === "missing") trouble = "Card setup incomplete";
if (paymentFlag === "ready" || paymentFlag === "failed" || paymentFlag === "cancelled" || paymentFlag === "missing") {
  history.replaceState({}, "", location.pathname);
}
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const policy = { allowed_categories: [...selectedCategories] };
  for (const input of form.elements) {
    if (!input.name) continue;
    if (input.type === "checkbox") policy[input.name] = input.checked;
    else policy[input.name] = input.value;
  }
  try { await send("PUT", "/policy", policy); $("settings").close(); poll(); }
  catch (error) { $("rules-error").textContent = error.message; }
});

poll();
setInterval(poll, 1000);
