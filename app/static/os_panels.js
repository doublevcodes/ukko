// The Restaurant OS panels around the buying agent: KPI cards, replenishment tiles, orders,
// activity and data sources. Every value comes from GET /state.
// The camera panel is in os_camera.js and the inventory table in os_inventory.js.

const $ = (id) => document.getElementById(id);
const drawn = {};  // the last HTML written to each element
// Write only when the HTML changed, so an unchanged panel keeps its scroll position and typed text.
function put(id, html) {
  if (drawn[id] !== html) { $(id).innerHTML = html; drawn[id] = html; }
}

const LEVEL_WORDS = { full: "Full", low: "Low", empty: "Empty" };
const LEVEL_STATUS = { full: "ok", low: "reorder", empty: "critical" };
const STOCK_WORDS = { critical: "Critical", reorder: "Reorder", unseen: "Not seen yet", ok: "OK" };
const euros = (value) => "€" + Math.round(Number(value)).toLocaleString("en-GB");
const percent = (fraction) => `${Math.round(fraction * 100)}%`;
const shelfStatus = (item) => (item.level ? LEVEL_STATUS[item.level] : "unseen");
const waitingForApproval = (state) => state.requests.filter((request) => request.decision.status === "awaiting_approval" && request.decision.chosen_plan);
// The class for a product's tile, camera reading and inventory row: "selected" once it was clicked in the video.
const picked = (item) => (item.product_key === selectedProductKey ? "selected" : "");

function drawKpis(state) {
  const low = state.shelf.filter((item) => item.level === "low" || item.level === "empty");
  const waiting = waitingForApproval(state);
  const proposed = waiting.reduce((sum, request) => sum + Number(request.decision.chosen_plan.total), 0);
  const limit = Number(state.policy.auto_approve_max_total_eur);
  const limits = state.standing_limits;
  const cards = [
    ["Items to reorder", low.length, low.length ? `of ${state.shelf.length} camera-watched` : "none at risk"],
    ["Proposed order", waiting.length ? money(proposed) : "—",
      waiting.length ? `awaiting approval · ${day(waiting[0].decision.chosen_plan.delivery_date)}` : "nothing awaiting approval"],
    ["Agent may spend alone", euros(limit),
      !limit ? "Safe mode: a person signs every order" : limits ? `per order · ${euros(limits.budget_eur)} budget, AP2 signed` : "per order · limits not signed yet"],
  ];
  put("kpis", cards.map(([label, value, note]) => `
    <div class="card kpi"><div class="kpi-label">${label}</div><div class="kpi-value">${value}</div><div class="small">${note}</div></div>`).join(""));
}

// One tile per product on the watched shelf.
function drawReplenishment(state) {
  put("replenishment", state.shelf.map((item) => `
    <div class="tile ${shelfStatus(item)} ${picked(item)}" data-product-key="${esc(item.product_key)}" title="${esc(item.name)}"><div class="tile-name">${esc(item.name)}</div>
      <b>${item.level ? LEVEL_WORDS[item.level] : "Not seen yet"}</b>
      <span class="small">${item.confidence != null ? `camera ${percent(item.confidence)}` : "waiting for the camera"}</span></div>`).join("")
    || `<p class="small">No products in config/shelf.yaml.</p>`);
}

function drawOrders(state) {
  const item = (id, total, note, badge) => `
    <div class="item"><div><b>${esc(id)}</b><div class="small">${note}</div></div>
      <div class="item-side"><b>${money(total)}</b><div>${badge}</div></div></div>`;
  const orders = state.requests.filter((request) => request.decision.order_confirmation || waitingForApproval(state).includes(request)).map((request) => {
    const order = request.decision.order_confirmation, plan = request.decision.chosen_plan;
    const lines = plan ? plan.lines.length : order.lines.length;
    const rest = `${lines} line${lines === 1 ? "" : "s"}${plan ? ` · delivery ${day(plan.delivery_date)}` : ""}`;
    return order
      ? item(order.order_id, order.total, `${order.flow === "human_present" ? "Manager approval" : "Agent, within its standing limits"} · ${rest}`,
        pill("ordered", order.receipts_verified ? "Ordered · receipts verified" : "Ordered"))
      : item(request.id, plan.total, `Proposed by the agent · ${rest}`, pill("awaiting_approval", "Awaiting approval"));
  });
  put("orders", orders.join("") || `<p class="small">No orders yet.</p>`);
}

function drawActivity(state) {
  put("activity", state.audit.map((entry) => `
    <div class="log"><span class="small">${clock(entry.timestamp)}</span>
      <div><div class="eyebrow">${esc(entry.step.replace("_", " "))}</div>${esc(entry.summary)}</div></div>`).join("")
    || `<p class="small">Nothing has happened yet.</p>`);
}

function drawSources(state) {
  const health = state.health || {};
  const badge = (ok, missing) => (ok ? pill("live", "Live") : pill("offline", missing));
  const sources = [
    ["Storage camera + vision model", "Shelf level per watched product, with a confidence score",
      state.shelf.some((item) => item.updated_at) ? pill("live", "Live") : pill("", "Waiting")],
    ["Shelf video (Gemini vision)", "Where each watched product stands in the camera picture, for the clickable boxes",
      !health.camera_connected ? pill("offline", "No video") : !health.gemini_key_set ? pill("offline", "No key")
        : (state.detections || []).length ? pill("live", "Live") : pill("", "Nothing recognised")],
    ["Valio Aimo prices and availability", "Signed live offers, pack sizes, delivery dates and order cut-offs", badge(state.standing_limits, "Not connected")],
    ["Web checks (Exa)", "Recalls, substitute suitability and price sanity, with sources", badge(health.exa_key_set, "No key")],
    ["Language model (Gemini)", "Reads typed requests and words the explanation", badge(health.gemini_key_set, "No key")],
    ["Approval rules + AP2 limits", "What the agent may order alone, signed by the manager key", badge(state.standing_limits, "Not signed")],
    ...BASELINE.plannedSources.map(([name, note]) => [name, note, pill("", "Planned")]),
  ];
  put("sources", sources.map(([name, note, status]) => `
    <div class="item"><div><b>${name}</b><div class="small">${note}</div></div><div class="item-side">${status}</div></div>`).join(""));
}
