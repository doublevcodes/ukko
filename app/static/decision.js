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
// Long keys / ids (offer ids, mandate refs, JWTs, key names) always render monospace.
const mono = (text) => `<span class="mono">${esc(text)}</span>`;
const monoOrDash = (text) => {
  const value = text == null || text === "" ? "—" : String(text);
  return value === "—" || value === "present" ? esc(value) : mono(value);
};

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
// True while the agent is still building a plan — show a skeleton instead of half-drawn folds.
const decisionPending = (request) => {
  const decision = request?.decision;
  return Boolean(decision && decision.status === "working" && !decision.chosen_plan && !decision.order_confirmation);
};
// What one sales unit is: "24-pack", "1 L", or the supplier's unit code when the size is unknown.
const packLabel = (offer) => {
  const pieces = Number(offer.pieces_per_sales_unit);
  if (pieces > 1) return `${pieces}-pack`;
  const named = /(?:^|\s)(\d+)\s*[x×]\s*\d/i.exec(offer.name || "") || /\d+(?:[.,]\d+)?\s*(?:ml|cl|l|g|kg)\s*[x×]\s*(\d+)\b/i.exec(offer.name || "");
  if (named) return `${named[1]}-pack`;
  if (offer.pack_size && offer.pack_unit) return `${amount(offer.pack_size)} ${offer.pack_unit === "KG" ? "kg" : "L"}`;
  return offer.sales_unit || "unit";
};

// Constraint tokens like FREE_FROM_LACTOSE → monospace; leave the rest as prose.
function formatConstraintText(text) {
  return esc(text).replace(/\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b/g, '<span class="mono">$1</span>');
}

function planTable(plan) {
  const lines = plan.lines.map((line) => {
    const note = line.substitute_reason || (line.offer.brand || "").trim();
    return `
      <div class="plan-line">
        <div class="plan-line-copy">
          <div class="plan-line-name">${esc(line.offer.name)}</div>
          ${line.is_substitute ? `<div class="plan-line-kind">Substitute</div>` : ""}
          ${note ? `<p class="plan-line-note">${formatConstraintText(note)}</p>` : ""}
        </div>
        <div class="plan-line-totals">
          <b class="plan-line-price">${money(line.line_total)}</b>
          <span class="plan-line-qty">${esc(line.quantity)} × ${esc(packLabel(line.offer))}</span>
        </div>
      </div>`;
  }).join("");
  const facts = [
    `${Math.round(Number(plan.coverage) * 100)}% covered`,
    plan.meets_deadline ? "On time" : "Late",
    `Delivery ${day(plan.delivery_date)}`,
  ].map((fact) => `<span class="fact">${esc(fact)}</span>`).join("");
  return `<div class="plan-lines">${lines}</div><div class="facts">${facts}</div>`;
}

// Total + optional approve/reject strip. Delivery already sits in the fact chips above.
function summaryHtml(decision, actions) {
  const plan = decision.chosen_plan;
  const order = decision.order_confirmation;
  const total = money(order ? order.total : plan?.total);
  return `
    <div class="order-bar">
      <div class="order-bar-total">
        <div class="order-value">${total}</div>
        <div class="small">excl. VAT · Valio Aimo</div>
      </div>
      ${actions || ""}
    </div>`;
}

function siteName(url) {
  try {
    const host = new URL(url).hostname.replace(/^www\./, "");
    return host || url;
  } catch {
    return url || "";
  }
}

function sourceHtml(source) {
  const href = /^https?:\/\//.test(source.url) ? source.url : "#";
  const site = siteName(source.url);
  const when = source.published_date || "date unknown";
  return `
    <a class="source" href="${esc(href)}" target="_blank" rel="noopener noreferrer">
      <span class="source-site">${esc(site)}</span>
      <span class="source-title">${esc(source.title || site || "Source")}</span>
      <span class="small">${esc(when)}</span>
    </a>`;
}

function findingsHtml(findings) {
  return `<div class="fold-stack">${findings.map((finding) => `
    <div class="fold-item finding">
      <div class="fold-item-head">
        <b>${esc(CHECK_WORDS[finding.check_type] || finding.check_type)}</b>
        ${pill(finding.severity, SEVERITY_WORDS[finding.severity])}
      </div>
      <p class="fold-item-body">${esc(finding.summary)}</p>
      ${finding.sources.length ? `<div class="source-list">${finding.sources.map(sourceHtml).join("")}</div>` : ""}
    </div>`).join("")}</div>`;
}

function confirmationHtml(order) {
  if (!order) return "";
  const who = order.flow === "human_present" ? "Signed by a person" : "Signed by the agent";
  const paid = order.payment_method ? ` · ${esc(order.payment_method)}` : "";
  return `
    <div class="confirmation">
      <b>Ordered <span class="mono">${esc(order.order_id)}</span></b>
      <span class="small"> · ${money(order.total)} · ${esc(who)}${paid}</span>
    </div>`;
}

function _offerFrom(request) {
  const plan = request.decision?.chosen_plan;
  return plan?.lines?.[0]?.offer || request.offers?.[0] || null;
}

function verificationHtml(request, isOpen = (key, fallback) => fallback) {
  const offer = _offerFrom(request);
  const order = request.decision?.order_confirmation;
  if (!offer && !order) return "";
  const claims = offer?.offer_claims || {};
  const checks = (order?.trust_checks?.length ? order.trust_checks : offer?.trust_checks) || [];
  const kid = (() => {
    const sig = checks.find((c) => c.name === "signature");
    const match = sig?.reason?.match(/\(([^)]+)\)/);
    return match ? match[1] : "valio-aimo-2026-11";
  })();
  const originBits = [
    offer?.origin_country ? ["Country", esc(offer.origin_country), false] : null,
    offer?.origin_producer ? ["Producer", esc(offer.origin_producer), false] : null,
    (offer?.origin_certificates || []).length
      ? ["Certificates", (offer.origin_certificates || []).map((c) => mono(c.id)).join(", "), false]
      : null,
  ].filter(Boolean);
  const chain = [
    ["Mandate", order?.mandate_id || order?.mandate_reference || "—"],
    ["Offer", offer?.offer_id || (order?.offer_ids || [])[0] || "—"],
    ["Order", order?.order_id || "—"],
    ["Receipt", order ? "present" : "—"],
  ];
  const issued = claims.iat ? dayTime(new Date(claims.iat * 1000).toISOString()) : (offer?.expires_at ? dayTime(offer.expires_at) : "—");
  const exp = claims.exp ? dayTime(new Date(claims.exp * 1000).toISOString()) : (offer?.expires_at ? dayTime(offer.expires_at) : "—");
  const passed = checks.filter((c) => c.passed).length;
  const allOk = checks.length > 0 && passed === checks.length;
  const verifyEnd = checks.length
    ? `<span class="fold-status ${allOk ? "ok" : "check_failed"}">${esc(`${passed}/${checks.length}`)}</span>`
    : "";
  // [label, html, isId] — id/key values use monospace.
  const meta = [
    ["Signer", mono(claims.iss || "valio-aimo-gateway"), true],
    ["Key", mono(kid), true],
    ["Offer id", monoOrDash(offer?.offer_id), true],
    ["Issued", esc(issued), false],
    ["Expires", esc(exp), false],
  ];
  const checkRows = checks.length
    ? checks.map((c) => `
      <div class="check-row ${c.passed ? "pass" : "fail"}">
        <span class="check-mark" aria-hidden="true">${c.passed ? "✓" : "✗"}</span>
        <div class="check-copy">
          <b>${esc(c.name)}</b>
          <p class="fold-item-body">${formatCheckReason(c.reason)}</p>
        </div>
      </div>`).join("")
    : `<p class="small">No checks recorded yet.</p>`;
  const metaRows = (rows) => `<dl class="meta-list">${rows.map(([k, html]) => `
    <div><dt>${esc(k)}</dt><dd>${html}</dd></div>`).join("")}</dl>`;
  const body = `
    <div class="fold-body">
      ${metaRows(meta)}
      <div class="fold-section">
        <div class="fold-label">Checks</div>
        <div class="fold-stack check-stack">${checkRows}</div>
      </div>
      <div class="fold-section">
        <div class="fold-label">Origin</div>
        ${originBits.length ? metaRows(originBits) : `<p class="small">Origin not stated by supplier</p>`}
      </div>
      <div class="fold-section">
        <div class="fold-label">Trust chain</div>
        <ol class="trust-chain">${chain.map(([k, v]) => `
          <li><span class="small">${esc(k)}</span>${monoOrDash(v)}</li>`).join("")}</ol>
      </div>
      ${offer?.offer_jwt ? `<details data-key="signed-offer" class="nested-fold" ${isOpen("signed-offer", false) ? "open" : ""}>
        <summary>Signed offer</summary>
        <div class="fold-body">
          <div class="fold-label">Token</div>
          <pre class="small trust-token mono">${esc(offer.offer_jwt)}</pre>
          <div class="fold-label">Claims</div>
          <pre class="small trust-token mono">${esc(JSON.stringify(claims, null, 2))}</pre>
        </div>
      </details>` : ""}
    </div>`;
  return `<details data-key="verification" class="verification" ${isOpen("verification", false) ? "open" : ""}>
    <summary>
      <span class="fold-summary-main">Verification${checks.length ? ` (${checks.length})` : ""}</span>
      ${verifyEnd ? `<span class="fold-summary-end">${verifyEnd}</span>` : ""}
    </summary>${body}</details>`;
}

// Reasons often embed a key name in parentheses — keep that bit monospace.
function formatCheckReason(reason) {
  const text = String(reason || "");
  const match = text.match(/^(.*?)(\(([^)]+)\))(.*)$/);
  if (!match) return esc(text);
  return `${esc(match[1])}<span class="mono">(${esc(match[3])})</span>${esc(match[4])}`;
}

function whyHtml(decision, isOpen) {
  const reasons = decision.policy_result?.reasons || [];
  if (!decision.explanation && !reasons.length) return "";
  const count = reasons.length || (decision.explanation ? 1 : 0);
  const open = isOpen("why", false);
  const body = `
    <div class="fold-body">
      ${decision.explanation ? `<p class="fold-lead">${esc(decision.explanation)}</p>` : ""}
      ${reasons.length ? `<div class="fold-stack">${reasons.map((reason) => `
        <div class="fold-item reason"><p class="fold-item-body">${esc(reason)}</p></div>`).join("")}</div>` : ""}
    </div>`;
  return `<details data-key="why" class="why" ${open ? "open" : ""}>
    <summary>Why${count ? ` (${count})` : ""}</summary>${body}</details>`;
}

function decisionSkeletonHtml(request) {
  const latest = request.timeline?.length ? request.timeline[request.timeline.length - 1] : null;
  const fold = () => `<div class="skel-fold"><span class="skel-line w35"></span><span class="skel-line w20"></span></div>`;
  return `
    <div class="decision-head">
      <h2>${esc(request.title || "Working on a proposal…")}</h2>
      ${statusPill("working")}
    </div>
    <p class="decision-progress">${latest ? esc(latest.summary) : "Looking up offers and checking the plan…"}</p>
    <div class="decision-skeleton" aria-hidden="true">
      <div class="skel-card">
        <div class="skel-card-row">
          <div class="skel-stack">
            <span class="skel-line w90"></span>
            <span class="skel-line w70"></span>
            <span class="skel-line w55"></span>
          </div>
          <div class="skel-stack skel-stack-end">
            <span class="skel-line w40"></span>
            <span class="skel-line w30"></span>
          </div>
        </div>
      </div>
      <div class="skel-facts">
        <span class="skel-chip"></span><span class="skel-chip"></span><span class="skel-chip"></span>
      </div>
      ${fold()}${fold()}${fold()}${fold()}
    </div>`;
}

function decisionFooterSkeletonHtml() {
  return `
    <div class="order-bar decision-skeleton" aria-hidden="true">
      <div class="skel-stack">
        <span class="skel-line skel-price"></span>
        <span class="skel-line w45"></span>
      </div>
      <div class="skel-actions">
        <span class="skel-chip skel-input"></span>
        <span class="skel-chip skel-btn"></span>
        <span class="skel-chip skel-btn"></span>
      </div>
    </div>`;
}

// `actions` is extra HTML (buttons, an answer box). `isOpen(key, fallback)` lets the caller
// remember which of the fold-out sections the reader opened; without it the defaults apply.
function decisionHtml(request, actions = "", isOpen = (key, fallback) => fallback) {
  if (decisionPending(request)) {
    return decisionSkeletonHtml(request) + (actions ? decisionFooterSkeletonHtml() : "");
  }
  const decision = request.decision;
  const plan = decision.chosen_plan;
  const findings = decision.web_findings;
  const worst = SEVERITY_ORDER.find((severity) => findings.some((finding) => finding.severity === severity));
  const fold = (key, title, count, body, open = false, end = "") => (count
    ? `<details data-key="${key}" ${isOpen(key, open) ? "open" : ""}>
        <summary>
          <span class="fold-summary-main">${title} (${count})</span>
          ${end ? `<span class="fold-summary-end">${end}</span>` : ""}
        </summary>
        <div class="fold-body">${body}</div>
      </details>` : "");
  const webEnd = worst
    ? `<span class="fold-status ${esc(worst)}">${esc(SEVERITY_WORDS[worst])}</span>`
    : "";
  const TIER_MEDAL = { best: "gold", better: "silver", good: "bronze" };
  const substitutes = `<div class="fold-stack">${decision.substitute_options.map((option) => {
    const medal = TIER_MEDAL[option.tier] || "bronze";
    const accent = option.used_in_plan ? " in-plan" : "";
    return `
    <div class="fold-item sub-rank ${medal}${accent}">
      <div class="fold-item-head">
        <b>${esc(option.name)}</b>
        <span class="fold-item-meta">${money(option.comparison_price)}</span>
      </div>
      ${option.reason ? `<p class="fold-item-body">${formatConstraintText(option.reason)}</p>` : ""}
    </div>`;
  }).join("")}</div>`;
  const alternatives = `<div class="fold-stack">${decision.alternatives.map((other) => `
    <div class="fold-item">
      <div class="fold-item-head">
        <b>${esc(other.plan.label || `Plan ${other.plan.id}`)}</b>
        <span class="fold-item-meta">${money(other.plan.total)}</span>
      </div>
      <p class="fold-item-body">${esc(other.why_not)}</p>
    </div>`).join("")}</div>`;
  const steps = `<ol class="timeline">${request.timeline.map((entry) => `
    <li>
      <div class="fold-item-head">
        <span class="step">${esc(entry.step.replaceAll("_", " "))}</span>
        <span class="small">${clock(entry.timestamp)}</span>
      </div>
      <p class="fold-item-body">${esc(entry.summary)}</p>
    </li>`).join("")}</ol>`;
  // Only embed the total strip when the caller passed actions (modals / phone).
  // The dashboard pins it in .decision-footer instead so it stays visible while details scroll.
  const summary = actions ? summaryHtml(decision, actions) : "";
  return `
    <div class="decision-head">
      <h2>${esc(request.title)}</h2>
      ${statusPill(decision.status)}
    </div>
    ${decision.message ? `<div class="message ${esc(decision.status)}">${esc(decision.message)}</div>` : ""}
    ${confirmationHtml(decision.order_confirmation)}
    ${plan ? planTable(plan) : ""}
    ${whyHtml(decision, isOpen)}
    ${verificationHtml(request, isOpen)}
    ${fold("web", "Web checks", findings.length, findingsHtml(findings), false, webEnd)}
    ${fold("substitutes", "Substitutes", decision.substitute_options.length, substitutes)}
    ${fold("alternatives", "Alternatives", decision.alternatives.length, alternatives)}
    ${fold("steps", "Steps", request.timeline.length, steps, false)}
    ${summary}`;
}
