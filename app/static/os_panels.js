// The Restaurant OS panels around the buying agent: replenishment tiles, orders,
// and the audit trail. Every value comes from GET /state.
// The camera panel is in os_camera.js and the inventory table in os_inventory.js.

const $ = (id) => document.getElementById(id);
const drawn = {};  // the last HTML written to each element
// Write only when the HTML changed, so an unchanged panel keeps its scroll position and typed text.
function put(id, html) {
  const el = $(id);
  if (!el) return;
  if (drawn[id] !== html) { el.innerHTML = html; drawn[id] = html; }
}

const LEVEL_WORDS = { full: "Full", low: "Low", empty: "Empty" };
const LEVEL_STATUS = { full: "ok", low: "reorder", empty: "critical" };
const STOCK_WORDS = {
  critical: "Critical", reorder: "Reorder", unseen: "Not seen yet", ok: "OK",
  ordered: "On order", rejected: "Rejected",
};
const euros = (value) => "€" + Math.round(Number(value)).toLocaleString("en-GB");
const percent = (fraction) => `${Math.round(fraction * 100)}%`;
// Vision quantity as a whole number of units (cans/bottles), for tiles, readings and inventory.
const qtyNum = (value) => {
  if (value == null || Number.isNaN(Number(value))) return null;
  return String(Math.max(0, Math.round(Number(value))));
};
const CONFIRM_LOOKS = 1; // must match app/vision.py CONFIRM_LOOKS
const COUNT_AGREE = 2;   // must match app/vision.py COUNT_AGREE
const waitingForApproval = (state) => state.requests.filter((request) => request.decision.status === "awaiting_approval" && request.decision.chosen_plan);
// Newest request for this shelf product (requests in /state are newest first).
const latestFor = (state, item) => state.requests.find((entry) => entry.product_key === item.product_key);
// Ordered or rejected for this low period (until the shelf reads full again).
const handledStatus = (state, item) => {
  if (item.level === "full") return null;
  const status = latestFor(state, item)?.decision.status;
  return (status === "ordered" || status === "rejected") ? status : null;
};
// Display status for tiles / inventory: ordered/rejected override low/empty urgency.
const shelfStatus = (state, item) => {
  const handled = handledStatus(state, item);
  if (handled) return handled;
  return item.level ? LEVEL_STATUS[item.level] : "unseen";
};
// Why a low/empty row has no live order yet (confirming count / already ordered / in progress).
function waitReason(state, item) {
  if (item.level !== "low" && item.level !== "empty") return null;
  const open = latestFor(state, item);
  const status = open?.decision.status;
  if (status === "ordered") return "Ordered";
  if (status === "rejected") return "Rejected";
  if (status === "awaiting_approval") return "Awaiting approval";
  if (status === "working") return "Working on a proposal";
  if (status === "needs_clarification") return "Needs a person to clarify";
  if (status === "failed") return "Reorder failed — will try again";
  if (open && ["working", "needs_clarification", "awaiting_approval"].includes(status)) {
    return STATUS_WORDS[status] || status;
  }
  if (!item.armed) return "Ordered — waiting for shelf refill";
  const streak = Number(item.low_streak) || 0;
  const recent = item.recent_counts || [];
  // Vision: fill COUNT_AGREE looks that agree (low/empty = same), then CONFIRM_LOOKS confirmed lows.
  if (recent.length < COUNT_AGREE) {
    return `Confirming count (${recent.length}/${COUNT_AGREE})`;
  }
  if (streak < CONFIRM_LOOKS) {
    return `Confirming low (${streak}/${CONFIRM_LOOKS})`;
  }
  return "Starting reorder…";
}
// The class for a product's tile, camera reading and inventory row: "selected" once it was clicked in the video.
const picked = (item) => (item.product_key === selectedProductKey ? "selected" : "");

// True when nothing on the shelf still needs a live reorder (stocked, or already ordered/rejected).
function shelfSettled(state) {
  return !(state.shelf || []).some((item) => (
    (item.level === "low" || item.level === "empty") && !handledStatus(state, item)
  ));
}

// Only low/critical items that still need attention (hide once ordered or rejected).
function drawReplenishment(state) {
  const needed = state.shelf.filter((item) => (
    (item.level === "low" || item.level === "empty") && !handledStatus(state, item)
  ));
  put("replenishment", needed.map((item) => {
    const status = shelfStatus(state, item);
    const why = waitReason(state, item);
    const seen = item.quantity != null && Number(item.quantity) > 0;
    const conf = seen && item.confidence != null ? ` · ${percent(item.confidence)} conf.`
      : (item.quantity != null && Number(item.quantity) <= 0 ? " · not seen" : "");
    const detail = why
      ? why
      : item.quantity != null
        ? `pcs estimated${conf}`
        : "waiting for the first look";
    return `
    <div class="tile ${status} ${picked(item)}" data-product-key="${esc(item.product_key)}" title="${esc(item.name)}"><div class="tile-name">${esc(item.name)}</div>
      <b>${item.quantity != null ? qtyNum(item.quantity) : (item.level ? LEVEL_WORDS[item.level] : "Not seen yet")}</b>
      <span class="small">${esc(detail)}</span></div>`;
  }).join("")
    || `<div class="empty-state">
      <b>Shelf looks stocked</b>
      <p class="small">Nothing needs replenishment. Take cartons off the shelf, or type a request below.</p>
    </div>`);
}

function drawOrders(state) {
  const item = (requestId, labelHtml, total, note, badge) => `
    <button type="button" class="item order-item" data-request-id="${esc(requestId)}">
      <div><b>${labelHtml}</b><div class="small">${note}</div></div>
      <div class="item-side"><b>${money(total)}</b><div>${badge}</div></div>
    </button>`;
  const pending = waitingForApproval(state);
  const orders = state.requests
    .filter((request) => request.decision.order_confirmation || pending.includes(request))
    // Approvals first so new completed orders do not bury them.
    .sort((a, b) => Number(b.decision.status === "awaiting_approval") - Number(a.decision.status === "awaiting_approval"))
    .map((request) => {
      const order = request.decision.order_confirmation, plan = request.decision.chosen_plan;
      const lines = plan ? plan.lines.length : order.lines.length;
      const rest = `${lines} line${lines === 1 ? "" : "s"}${plan ? ` · delivery ${day(plan.delivery_date)}` : ""}`;
      return order
        ? item(request.id, `<span class="mono">${esc(order.order_id)}</span>`, order.total,
          `${order.flow === "human_present" ? "Manager approval" : "Agent, within its standing limits"} · ${rest}`,
          pill("ordered", order.receipts_verified ? "Ordered · receipts verified" : "Ordered"))
        : item(request.id, `<span class="mono">${esc(request.id)}</span>`, plan.total, `Proposed by the agent · ${rest}`,
          pill("awaiting_approval", "Awaiting approval"));
    });
  put("orders", orders.join("") || `<p class="small">No orders yet.</p>`);
}

function drawActivity(state) {
  put("activity", state.audit.map((entry) => `
    <div class="log"><span class="small">${clock(entry.timestamp)}</span>
      <div><div class="eyebrow">${esc(entry.step.replace("_", " "))}</div>${esc(entry.summary)}</div></div>`).join("")
    || `<p class="small">Nothing has happened yet.</p>`);
}
