// The inventory table: the camera-watched shelf (live, from GET /state), with search and a status filter.
// Laptop vision estimates how many units are visible; the level gauge is derived from that count.

const STATUS_RANK = { critical: 0, reorder: 1, unseen: 2, ok: 3 };

// Three bars: empty lights one, low two, full three.
function levelGauge(item) {
  const lit = { empty: 1, low: 2, full: 3 }[item.level] || 0;
  return `<span class="gauge ${shelfStatus(item)}">${[1, 2, 3].map((bar) => `<i class="${bar <= lit ? "on" : ""}"></i>`).join("")}</span>`;
}

// What the agent proposes for a watched product: its current plan, or else the reorder setting.
function proposedFor(state, item) {
  const request = state.requests.find((entry) => entry.product_key === item.product_key && entry.decision.chosen_plan);
  const status = request?.decision.status;
  if (status === "awaiting_approval" || status === "working" || (status === "ordered" && item.level !== "full")) {
    const packs = request.decision.chosen_plan.lines.map((line) => `${line.quantity} × ${packLabel(line.offer)}`).join(" + ");
    return `${esc(packs)}<div class="small">${esc(STATUS_WORDS[status])}</div>`;
  }
  return item.reorder_quantity
    ? `<span class="small">${amount(item.reorder_quantity)} ${UNIT_WORDS[item.unit] || ""} when low</span>` : "—";
}

function drawInventory(state) {
  const search = $("inventory-search").value.trim().toLowerCase();
  const status = $("inventory-status").value;
  // Most urgent first.
  const shown = state.shelf.filter((item) => item.name.toLowerCase().includes(search) && (!status || shelfStatus(item) === status))
    .sort((a, b) => STATUS_RANK[shelfStatus(a)] - STATUS_RANK[shelfStatus(b)]);
  put("inventory-body", shown.map((item) => `
    <tr data-product-key="${esc(item.product_key)}" class="${picked(item)}">
      <td><b>${esc(item.name)}</b><div class="small">Valio product ${esc(item.product_id || "—")}</div></td>
      <td>${pill(shelfStatus(item), STOCK_WORDS[shelfStatus(item)])}</td>
      <td>${levelGauge(item)} <b>${item.quantity != null ? qtyNum(item.quantity) : (item.level ? LEVEL_WORDS[item.level] : "—")}</b>
        ${item.quantity != null ? `<span class="small">pcs</span>` : ""}</td>
      <td class="num">${item.confidence != null ? percent(item.confidence) : "—"}</td>
      <td>${item.updated_at ? `${clock(item.updated_at)}<div class="small">${esc(item.device_id)}</div>` : `<span class="small">waiting for the first look</span>`}</td>
      <td class="num">${proposedFor(state, item)}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="small">No products match.</td></tr>`);
}
