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

const pill = (kind, text) => `<span class="pill ${esc(kind)}">${esc(text)}</span>`;
const statusPill = (status) => pill(status, STATUS_WORDS[status] || status);
// What one sales unit is: "24-pack", "1 L", or the supplier's unit code when the size is unknown.
const packLabel = (offer) => {
  const pieces = Number(offer.pieces_per_sales_unit);
  if (pieces > 1) return `${pieces}-pack`;
  const named = /(?:^|\s)(\d+)\s*[x×]\s*\d/i.exec(offer.name || "") || /\d+(?:[.,]\d+)?\s*(?:ml|cl|l|g|kg)\s*[x×]\s*(\d+)\b/i.exec(offer.name || "");
  if (named) return `${named[1]}-pack`;
  if (offer.pack_size && offer.pack_unit) return `${amount(offer.pack_size)} ${offer.pack_unit === "KG" ? "kg" : "L"}`;
  return offer.sales_unit || "unit";
};

function planTable(plan) {
  const rows = plan.lines.map((line) => `
    <tr>
      <td><b>${esc(line.offer.name)}</b> ${line.is_substitute ? pill("substitute", "Sub") : ""}
        <div class="small">${esc((line.offer.brand || "").trim())}</div></td>
      <td class="num">${esc(line.quantity)} × ${esc(packLabel(line.offer))}</td>
      <td class="num"><b>${money(line.line_total)}</b></td>
    </tr>`).join("");
  const facts = [
    `${Math.round(Number(plan.coverage) * 100)}% covered`,
    plan.meets_deadline ? "On time" : "Late",
    `Delivery ${day(plan.delivery_date)}`,
  ].map((fact) => `<span class="fact">${esc(fact)}</span>`).join("");
  return `
    <div class="table-wrap"><table class="plan">
      <tbody>${rows}</tbody>
    </table></div>
    <div class="facts">${facts}</div>`;
}

// Compact total + CTA. Delivery already sits in the fact chips above.
function summaryHtml(decision, actions) {
  const plan = decision.chosen_plan;
  const order = decision.order_confirmation;
  return `
    <div class="order-summary">
      <div>
        <div class="order-value">${money(order ? order.total : plan.total)}</div>
        <div class="small">excl. VAT · Valio Aimo</div>
      </div>
      ${actions}
    </div>`;
}

function findingsHtml(findings) {
  return findings.map((finding) => `
    <div class="finding">
      <div><b>${esc(CHECK_WORDS[finding.check_type] || finding.check_type)}</b> ${pill(finding.severity, SEVERITY_WORDS[finding.severity])}</div>
      <div>${esc(finding.summary)}</div>
      ${finding.sources.map((source) => `
        <div class="source">
          <a href="${esc(/^https?:\/\//.test(source.url) ? source.url : "#")}" target="_blank" rel="noopener noreferrer">${esc(source.title)}</a>
        </div>`).join("")}
    </div>`).join("");
}

function confirmationHtml(order) {
  if (!order) return "";
  const who = order.flow === "human_present" ? "Signed by a person" : "Signed by the agent";
  return `
    <div class="confirmation">
      <b>Ordered ${esc(order.order_id)}</b>
      <span class="small"> · ${money(order.total)} · ${esc(who)}</span>
    </div>`;
}

function whyHtml(decision, isOpen) {
  const reasons = decision.policy_result?.reasons || [];
  const needsReasons = decision.status === "awaiting_approval" || decision.status === "rejected";
  const body = [
    decision.explanation ? `<p>${esc(decision.explanation)}</p>` : "",
    reasons.length ? `<ul>${reasons.map((reason) => `<li>${esc(reason)}</li>`).join("")}</ul>` : "",
  ].join("");
  if (!body) return "";
  if (needsReasons && reasons.length) {
    return `<div class="why">${body}</div>`;
  }
  const open = isOpen("why", false);
  return `<details data-key="why" class="why" ${open ? "open" : ""}><summary>Why</summary>${body}</details>`;
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
      <h2>${esc(request.title)}</h2>
      ${statusPill(decision.status)}
    </div>
    ${decision.message ? `<div class="message ${esc(decision.status)}">${esc(decision.message)}</div>` : ""}
    ${confirmationHtml(decision.order_confirmation)}
    ${plan ? planTable(plan) : ""}
    ${plan ? summaryHtml(decision, actions) : actions}
    ${whyHtml(decision, isOpen)}
    ${fold("web", `Web checks ${worst ? pill(worst, SEVERITY_WORDS[worst]) : ""}`, findings.length, findingsHtml(findings), false)}
    ${fold("substitutes", "Substitutes", decision.substitute_options.length, list(decision.substitute_options.map((option) =>
      `${pill("tier", option.tier)} ${esc(option.name)} (${money(option.comparison_price)})${option.used_in_plan ? " — in plan" : ""}`)))}
    ${fold("alternatives", "Alternatives", decision.alternatives.length, list(decision.alternatives.map((other) =>
      `<b>Plan ${esc(other.plan.id)}</b>: ${esc(other.plan.label)}, ${money(other.plan.total)}. ${esc(other.why_not)}`)))}
    ${fold("steps", "Steps", request.timeline.length, `<ul class="timeline">${request.timeline.map((entry) =>
      `<li><span class="step">${esc(entry.step.replace("_", " "))}</span> <span class="small">${clock(entry.timestamp)}</span><br>${esc(entry.summary)}</li>`).join("")}</ul>`,
      decision.status === "working")}`;
}
