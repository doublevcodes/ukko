// The inventory table: the camera-watched shelf (live, from GET /state), with search and a status filter.
// Laptop vision estimates how many units are visible; reorder uses the usage-based model.

const STATUS_RANK = { critical: 0, reorder: 1, rejected: 2, ordered: 3, unseen: 4, ok: 5 };

// Three bars: empty lights one, low two, full three. Colour follows visible qty, not order status.
function levelGauge(item) {
  const lit = { empty: 1, low: 2, full: 3 }[item.level] || 0;
  const stock = item.level ? LEVEL_STATUS[item.level] : "unseen";
  return `<span class="gauge ${stock}">${[1, 2, 3].map((bar) => `<i class="${bar <= lit ? "on" : ""}"></i>`).join("")}</span>`;
}

// One-line usage model: "3.2/d · 4.1d cover · R 12" with source + on-order under it.
function modelMetrics(item) {
  const usage = item.usage_per_day != null ? `${Number(item.usage_per_day).toFixed(1)}/d` : "—/d";
  const cover = item.days_of_cover != null ? `${Number(item.days_of_cover).toFixed(1)}d` : "—";
  const r = item.reorder_point != null ? `R ${amount(item.reorder_point)}` : "R —";
  const source = item.usage_observations > 0
    ? `measured ×${item.usage_observations}`
    : "assumed";
  const onOrder = item.on_order_units != null ? amount(item.on_order_units) : "0";
  const due = item.delivery_due ? " · due" : "";
  return `<div class="inv-model"><b>${esc(usage)}</b>`
    + `<span class="small"> · ${esc(cover)} · ${esc(String(r))}</span>`
    + `<div class="small">${esc(source)} · on order ${esc(String(onOrder))}${esc(due)}</div></div>`;
}

// Short product label for the proposed column (drop pack-size noise).
function shortOfferName(offer) {
  const raw = (offer?.name || "").trim().replace(/\s+/g, " ");
  if (!raw) return "";
  const cut = raw.replace(/\s+\d+(?:[.,]\d+)?\s*(?:ml|cl|l|g|kg)\b.*$/i, "").trim();
  const text = cut || raw;
  return text.length > 36 ? `${text.slice(0, 34)}…` : text;
}

// What the agent proposes for a watched product: its current plan, a wait reason, or model qty.
function proposedFor(state, item) {
  const request = latestFor(state, item);
  const decision = request?.decision;
  const status = decision?.status;
  const plan = decision?.chosen_plan;
  const order = decision?.order_confirmation;

  if (status === "ordered" && (plan || order)) {
    const lines = (plan?.lines || []).map((line) => {
      const name = shortOfferName(line.offer);
      return `<div class="inv-line"><b>${esc(`${line.quantity} × ${packLabel(line.offer)}`)}</b>`
        + (name ? `<span class="small">${esc(name)}</span>` : "") + `</div>`;
    }).join("");
    const total = order?.total != null ? money(order.total) : (plan ? money(plan.total) : null);
    const meta = [
      total,
      plan?.delivery_date ? day(plan.delivery_date) : null,
    ].filter(Boolean).join(" · ");
    return `<div class="inv-proposed">${lines}`
      + (meta ? `<div class="inv-meta"><span class="small">${esc(meta)}</span></div>` : "")
      + `</div>`;
  }
  if (status === "rejected") {
    return `<div class="inv-proposed">${pill("rejected", "Rejected")}</div>`;
  }
  if ((status === "awaiting_approval" || status === "working") && plan) {
    const packs = plan.lines.map((line) => `${line.quantity} × ${packLabel(line.offer)}`).join(" + ");
    return `<div class="inv-proposed"><b>${esc(packs)}</b>`
      + `<div class="small">${esc(STATUS_WORDS[status])}</div></div>`;
  }
  const why = waitReason(state, item);
  if (why) return `<span class="small">${esc(why)}</span>`;
  const qty = item.model_order_quantity != null ? amount(item.model_order_quantity) : "—";
  return `<span class="small">model would order ${esc(String(qty))}</span>`;
}

// Match the status filter: ordered/rejected low items still match Reorder/Critical so they
// do not disappear from the table after an order lands.
function matchesInventoryStatus(state, item, status) {
  if (!status) return true;
  const display = shelfStatus(state, item);
  if (display === status) return true;
  const stock = item.level ? LEVEL_STATUS[item.level] : "unseen";
  const handled = handledStatus(state, item);
  if (handled && (status === "reorder" || status === "critical") && stock === status) return true;
  return false;
}

function drawInventory(state) {
  const search = $("inventory-search").value.trim().toLowerCase();
  const status = $("inventory-status").value;
  // Always keep every watched food, including after order/reject, until the shelf reads full.
  // A product clicked in the camera overlay is pinned to the top until the selection clears.
  const shown = state.shelf
    .filter((item) => item.name.toLowerCase().includes(search) && matchesInventoryStatus(state, item, status))
    .sort((a, b) => {
      if (selectedProductKey) {
        if (a.product_key === selectedProductKey) return -1;
        if (b.product_key === selectedProductKey) return 1;
      }
      return STATUS_RANK[shelfStatus(state, a)] - STATUS_RANK[shelfStatus(state, b)];
    });
  put("inventory-body", shown.map((item) => {
    const display = shelfStatus(state, item);
    return `
    <tr data-product-key="${esc(item.product_key)}" class="${picked(item)}">
      <td><b>${esc(item.name)}</b><div class="small">${catalogueLabel(item)}</div></td>
      <td>${pill(display, STOCK_WORDS[display] || display)}</td>
      <td>${levelGauge(item)} <b>${item.quantity != null ? qtyNum(item.quantity) : (item.level ? LEVEL_WORDS[item.level] : "—")}</b>
        ${item.quantity != null ? `<span class="small">visible</span>` : ""}
        ${item.on_hand_units != null ? `<div class="small">${esc(String(amount(item.on_hand_units)))} units</div>` : ""}</td>
      <td class="num">${confidenceLabel(item)}</td>
      <td>${modelMetrics(item)}</td>
      <td class="proposed">${proposedFor(state, item)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="small">No products match.</td></tr>`);
}

// Valio catalogue mapping for a watched shelf food.
function catalogueLabel(item) {
  if (item.product_id) return `Valio <span class="mono">${esc(item.product_id)}</span>`;
  return "Closest catalogue match";
}

// Camera confidence: real model score when seen; "not seen" when absent (never show a fake %).
// Last reading lives in the title tooltip so the table stays narrow.
function confidenceLabel(item) {
  const tip = item.updated_at
    ? `Last reading ${clock(item.updated_at)}${item.device_id ? ` · ${item.device_id}` : ""}`
    : "Waiting for the first look";
  const title = ` title="${esc(tip)}"`;
  if (item.quantity != null && Number(item.quantity) <= 0) {
    return `<span class="small"${title}>not seen</span>`;
  }
  if (item.confidence != null && (item.quantity == null || Number(item.quantity) > 0)) {
    return `<span${title}>${percent(item.confidence)}</span>`;
  }
  if (item.level && item.quantity == null) {
    return `<span class="small"${title}>not seen</span>`;
  }
  return `<span${title}>—</span>`;
}
