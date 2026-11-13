// Turns one request (as sent by GET /state) into HTML.
// Shared by the dashboard (index.html) and the phone approval page (approve.html).
// Every piece of text from the server goes through esc(): product names and web
// snippets come from outside and must never be treated as HTML.

const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (value) => (value == null ? "–" : "€" + Number(value).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const amount = (value) => Number(value).toLocaleString("en-GB", { maximumFractionDigits: 1 });
const day = (iso) => (iso ? new Date(iso + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }) : "–");
const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
const dayTime = (iso) => (iso ? new Date(iso).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "–");

const STATUS_WORDS = {
  working: "Working…", needs_clarification: "Question for you", awaiting_approval: "Needs approval",
  ordered: "Ordered", rejected: "Rejected", failed: "Failed",
};
const CHECK_WORDS = { recall: "Recall check", substitute_suitability: "Substitute check", price_sanity: "Price check", other: "Web check" };
const SEVERITY_WORDS = { none: "No concern", caution: "Caution", blocker: "Blocker", check_failed: "Check failed" };
const SEVERITY_ORDER = ["blocker", "check_failed", "caution", "none"];  // worst first
const UNIT_WORDS = { l: "L", kg: "kg", pcs: "pcs" };

const pill = (kind, text) => `<span class="pill ${esc(kind)}">${esc(text)}</span>`;
const statusPill = (status) => pill(status, STATUS_WORDS[status] || status);
// What one sales unit is: "1 L", "0.5 kg", or the supplier's own unit code when the size is unknown.
const packLabel = (offer) => (offer.pack_size && offer.pack_unit
  ? `${amount(offer.pack_size)} ${offer.pack_unit === "KG" ? "kg" : "L"}` : offer.sales_unit || "unit");

function planTable(plan, unit) {
  const rows = plan.lines.map((line) => `
    <tr>
      <td><b>${esc(line.offer.name)}</b> ${line.is_substitute ? pill("substitute", "Substitute · " + (line.substitute_tier || "")) : ""}
        <div class="small">${esc([(line.offer.brand || "").trim(), "delivery " + day(line.offer.earliest_delivery)].filter(Boolean).join(" · "))}</div>
        ${line.substitute_reason ? `<div class="small">${esc(line.substitute_reason)}</div>` : ""}</td>
      <td class="num muted">${Number(line.covers) ? `${amount(line.covers)} ${esc(UNIT_WORDS[unit] || "")}` : "–"}</td>
      <td class="num">${esc(line.quantity)} × ${esc(packLabel(line.offer))}</td>
      <td class="num">${money(line.offer.unit_price)}</td>
      <td class="num"><b>${money(line.line_total)}</b></td>
    </tr>`).join("");
  const facts = [
    `Covers ${Math.round(Number(plan.coverage) * 100)}% of the request`,
    plan.meets_deadline ? "On time" : "Late",
    plan.reliability != null ? `Delivery reliability ${Math.round(plan.reliability * 100)}%` : null,
    plan.co2_kg != null ? `About ${Number(plan.co2_kg).toFixed(0)} kg CO₂` : null,
  ].filter(Boolean).map((fact) => `<span class="fact">${esc(fact)}</span>`).join("");
  return `
    <div class="table-wrap"><table class="plan">
      <thead><tr><th>Product</th><th class="num">Need covered</th><th class="num">Order</th><th class="num">Unit price</th><th class="num">Value</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <div class="facts">${facts}</div>`;
}

// The order value, who supplies it and how it is paid. `actions` (buttons, QR code) sits beside it.
function summaryHtml(decision, actions) {
  const plan = decision.chosen_plan;
  const order = decision.order_confirmation;
  const cutoffs = plan.lines.map((line) => line.offer.order_cutoff).filter(Boolean).sort();
  const meta = [
    ["Supplier", "Valio Aimo"],
    ["Delivery", `${day(plan.delivery_date)} (${plan.meets_deadline ? "on time" : "late"})`],
    ["Cutoff", cutoffs.length ? dayTime(cutoffs[0]) : "–"],
    ["Payment", order ? order.payment_method || "–" : "Invoice on account · AP2 signed mandate"],
  ];
  return `
    <div class="order-summary">
      <div>
        <div class="eyebrow">${order ? "Order value" : "Estimated order value"} · excluding VAT</div>
        <div class="order-value">${money(order ? order.total : plan.total)}</div>
        <dl class="meta">${meta.map(([name, value]) => `<dt>${name}</dt><dd>${esc(value)}</dd>`).join("")}</dl>
      </div>
      ${actions}
    </div>`;
}

function findingsHtml(findings) {
  return findings.map((finding) => `
    <div class="finding">
      <div><b>${esc(CHECK_WORDS[finding.check_type] || finding.check_type)}</b> ${pill(finding.severity, SEVERITY_WORDS[finding.severity])}</div>
      <div>${esc(finding.summary)}</div>
      ${finding.query ? `<div class="small">Searched: “${esc(finding.query)}”</div>` : ""}
      ${finding.sources.map((source) => `
        <div class="source">
          <a href="${esc(/^https?:\/\//.test(source.url) ? source.url : "#")}" target="_blank" rel="noopener noreferrer">${esc(source.title)}</a>
          <span class="small">${esc((source.published_date || "").slice(0, 10))}</span>
          <div class="small">“${esc(source.snippet)}”</div>
        </div>`).join("")}
    </div>`).join("");
}

function confirmationHtml(order) {
  if (!order) return "";
  const who = order.flow === "human_present" ? "Signed by a person (AP2, human present)" : "Signed by the agent within its standing limits (AP2, human not present)";
  return `
    <div class="confirmation">
      <h3>Order placed: ${esc(order.order_id)}</h3>
      <div>${money(order.total)} · ${esc(order.payment_method)}</div>
      <div>${esc(who)}</div>
      <div class="small">Mandate reference ${esc(order.mandate_reference.slice(0, 20))}… ·
        ${order.receipts_verified ? "supplier's signed receipts verified ✓" : "receipts not re-checked"}</div>
    </div>`;
}

// `actions` is extra HTML (buttons, an answer box). `isOpen(key, fallback)` lets the caller
// remember which of the fold-out sections the reader opened; without it the defaults apply.
function decisionHtml(request, actions = "", isOpen = (key, fallback) => fallback) {
  const decision = request.decision;
  const plan = decision.chosen_plan;
  const findings = decision.web_findings;
  const worst = SEVERITY_ORDER.find((severity) => findings.some((finding) => finding.severity === severity));
  const list = (items) => `<ul>${items.map((item) => `<li>${item}</li>`).join("")}</ul>`;
  const fold = (key, title, count, body, open = false) => (count
    ? `<details data-key="${key}" ${isOpen(key, open) ? "open" : ""}><summary>${title} (${count})</summary>${body}</details>` : "");
  return `
    <div class="decision-head">
      <div><div class="eyebrow">AI procurement decision · ${request.source === "sensor" ? "from the shelf camera" : "typed request"}</div>
        <h2>${esc(request.title)}</h2></div>
      ${statusPill(decision.status)}
    </div>
    ${decision.message ? `<div class="message ${esc(decision.status)}">${esc(decision.message)}</div>` : ""}
    ${confirmationHtml(decision.order_confirmation)}
    ${plan ? `<div class="plan-label">Plan ${esc(plan.id)} · ${esc(plan.label)}</div>${planTable(plan, request.request?.unit)}` : ""}
    ${decision.explanation || decision.policy_result ? `
      <div class="why"><div class="eyebrow">Why</div>
        ${decision.explanation ? `<p>${esc(decision.explanation)}</p>` : ""}
        ${decision.policy_result ? list(decision.policy_result.reasons.map(esc)) : ""}</div>` : ""}
    ${plan ? summaryHtml(decision, actions) : actions}
    ${fold("web", `Web checks, live via Exa ${worst ? pill(worst, SEVERITY_WORDS[worst]) : ""}`, findings.length, findingsHtml(findings), true)}
    ${fold("substitutes", "Substitutes ranked: best, better, good", decision.substitute_options.length, list(decision.substitute_options.map((option) =>
      `${pill("tier", option.tier)} ${esc(option.name)} (${money(option.comparison_price)} per unit)${option.used_in_plan ? " — used in the plan" : ""}
       <div class="small">${esc(option.reason)}</div>`)))}
    ${fold("alternatives", "Alternatives considered", decision.alternatives.length, list(decision.alternatives.map((other) =>
      `<b>Plan ${esc(other.plan.id)}</b>: ${esc(other.plan.label)}, ${money(other.plan.total)}. <i>Why not:</i> ${esc(other.why_not)}`)))}
    ${fold("steps", "What the agent did, step by step", request.timeline.length, `<ul class="timeline">${request.timeline.map((entry) =>
      `<li><span class="step">${esc(entry.step.replace("_", " "))}</span> <span class="small">${clock(entry.timestamp)}</span><br>${esc(entry.summary)}</li>`).join("")}</ul>`,
      decision.status === "working")}`;
}
