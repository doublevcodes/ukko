# Ukko — an open agent interface for Valio Aimo wholesale

**Ukko** is a working prototype of how a European food-service wholesaler can be safely readable by external purchasing agents — without exposing production systems or locking buyers into a single platform’s black box. For Junction 2026 we mock today’s Valio Aimo webshop/backend and define the **interface and operating model** an outside agent would use: look up products, compare live offers, place fixed-price orders, and escalate when authority ends.

## Two sides, one open contract

| Side | Role |
|---|---|
| **Valio agent gateway** | Supplier façade over the mock Valio API: catalogue search, **signed live offers**, mock merchant checkout, supplier audit log, OpenAPI docs, and the **same surface as MCP tools**. |
| **Ukko buying agent** | Kitchen-side agent: camera/typed intake → lookup → plans → web checks → policy → order or human approval. It talks to the gateway **only over HTTP**, like any third-party agent. |

Live demo: [ukko-delta.vercel.app](https://ukko-delta.vercel.app) · Gateway `/gateway` · MCP `/.well-known/mcp.json` · JWKS `/.well-known/jwks.json`

## End-to-end flow (core challenge)

1. **Need** — Shelf camera (or typed request, e.g. “1,000 litres of lactose-free milk by Monday”) creates a structured order request with hard constraints and soft preferences. Reorder size uses a transparent inventory model (cover + lead time + safety buffer, pack rounding).
2. **Lookup** — Gateway search + live offers: product identity (incl. GTIN when present), pack size, unit price excl. VAT, stock, earliest delivery, reliability, sustainability labels/origin from catalogue data.
3. **Match & plan** — Plain code (not the LLM) picks the “exact” SKU by constraint fit and plainness — **price never defines what exact means** — then builds ranked plans:
   - **A** — exact SKU, on time, as much as stock allows  
   - **B** — exact + **good/better/best** substitutes for the shortfall  
   - **C** — exact SKU after the deadline  
   Hard requirements (e.g. lactose-free, allergen rules) are never broken; preferences (organic, brand) influence ranking only.
4. **Guardrails** — Policy decides auto-order vs human sign-off: spend limit, substitutes, late delivery, unusual quantity vs usual, category allow-list, failed or caution web checks. Safe mode forces every order to a person; Autopilot restores the prior limit. Rules are editable on the dashboard and persisted.
5. **Commit** — [Agent Payments Protocol (AP2)](https://github.com/google-agentic-commerce/AP2) mandates authorise the cart (standing manager limits + per-order agent signature, or manager signature when a human approves). Stripe test mode charges the saved kitchen card; the gateway **re-prices** before commit and refuses price/stock drift. Buyer dashboard and supplier gateway both show the same order and trail.

## Trust protocol (verifiable claims)

Every price/stock/origin claim arrives as a short-lived **JWT/JWS (ES256)**. Public keys are published at `/.well-known/jwks.json`. Seven verification checks: signature, freshness, issuer, audience, exact cart match, no `jti` reuse, origin when the request requires it. A pass proves **who claimed what, unaltered and fresh** — not that the claim is absolutely true. Failed crypto or issuer checks **block** the offer; a person cannot approve around a forged signature.

## Autonomy, controls & audit

- **Rules are deterministic code** (`config/policy.yaml`). The LLM only parses natural language, helps select catalogue hits, summarises *sourced* web findings, and words the explanation — it never decides spend authority.
- Auto-approve only when *every* rule passes; otherwise phone or dashboard approval with a plain-language “why”.
- Append-only **human-readable audit** for buyer and Valio Aimo: offers seen, plans compared, web sources, policy reasons, AP2 presence (`human_present` / `human_not_present`), and payment outcome.

## Exceptions beyond the happy path

Partial availability → Plan B with tiered substitutes (Valio-listed or same brand+group → same group → other group still meeting hard rules). Late cut-off or missed deadline → Plan C or escalation. Checkout drift → refuse and re-plan. Web findings without a source are rejected by code; a check that cannot run fails closed and needs approval — never silently skipped.

## Openness & path to production

Open standards end-to-end: **HTTP + OpenAPI**, **MCP** tools (`search_products`, `get_offers`, checkout), **AP2** standing/cart mandates, **JWT/JWKS** offer trust. Assumptions and limits are explicit (`TRUST.md`, README). Credible next steps: supplier-hosted keys with rotation, certificate attestations (e.g. W3C Verifiable Credentials), invoice-on-account beside PSP demos, and real customer onboarding — so Valio Aimo stays agent-readable on open terms while owning the customer relationship.

*All data is synthetic. No production Valio systems or confidential data.*
