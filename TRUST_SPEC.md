# Ukko: trust protocol for supplier claims (spec for Cursor)

**How to use this file (humans):** save it as `TRUST_SPEC.md` in the project root, then tell Cursor:

> Read TRUST_SPEC.md fully. Do step T0 only, then stop.

---

## 1. Context

Ukko is a kitchen restocking agent built at a hackathon. A buying agent looks up offers from a supplier gateway (`valio-aimo-gateway`, our wrapper around Valio's mock API), runs web checks, applies spending rules, and orders or asks a person to approve.

The challenge sponsor (Valio Aimo) asks for a **trust protocol: the minimum data and verification standard that lets an external agent trust the supplier's claims on stock, price and origin without a manual check.**

Today the audit trail says offers are "signed" and receipts are "checked", but the dashboard shows no evidence of it, origin is not covered, nothing shows a bad offer being refused, and no standard is written down. This spec closes those four gaps.

We are a small team with mixed coding experience and a deadline tomorrow morning. Write simple, readable Python. Make small changes. Do not refactor unrelated code. Another teammate is changing the reorder model at the same time (`REORDER_SPEC.md`): do not touch that work.

## 2. How it works, in plain words

1. The supplier publishes a public key at a fixed address.
2. Every claim about price, stock and origin arrives as a signed offer with a short expiry time.
3. The agent orders on its own only if the signature is valid, the offer is fresh, and the order matches the offer exactly. Otherwise the offer is refused or a person decides.
4. The supplier's signed receipt quotes the offer, so every order can be traced from the manager's signed limits to the offer, the order and the receipt.

A signature proves **who** made a claim and that it **has not been altered**. It does not prove the claim is true. This limit must be stated in the docs and never hidden.

## 3. Open standards to use

| Purpose | Standard |
|---|---|
| Signed offer and receipt | JSON Web Token (RFC 7519) signed as a JWS (RFC 7515) |
| Signature algorithm | Ed25519, named `EdDSA` in tokens (RFC 8037) |
| Publishing the key | JWK Set (RFC 7517) at `/.well-known/jwks.json` |
| Product identity | GTIN where the catalogue data has one, plus the supplier's own product number |

Use a maintained library (PyJWT with `cryptography`, or whatever the project already uses for signing). **Never write cryptography by hand.** Check the library's current documentation before using an API.

## 4. Part 1: the published key (supplier side)

- Generate one Ed25519 key pair for the gateway on first start. Store the private key outside git (for example a `.keys/` folder listed in `.gitignore`, or an environment variable). Never log it, never commit it, never send it to the browser.
- Give the key an ID (`kid`), for example `valio-aimo-2026-11`. The ID is what makes replacing a key later possible.
- Serve the public key as a JWK Set at `GET /.well-known/jwks.json`.
- **The buyer side must never import the private key or any signing code.** It may only use what an outside agent could get: the token and the published key. This separation is the whole point, so keep it strict.

## 5. Part 2: the signed offer (the minimum data standard)

Every offer the gateway returns carries a signed token whose payload has these fields. All of them are covered by the signature.

| Group | Field | Meaning | Why it is in the minimum set |
|---|---|---|---|
| Identity | `iss` | Who is making the claim (`valio-aimo-gateway`) | The agent must know whose claim it is |
| | `aud` | The buyer the offer was made for | Stops an offer made to one kitchen being used by another |
| | `jti` | Unique offer ID | Lets an order quote the offer, and stops reuse |
| Freshness | `iat`, `exp` | When issued, when it stops being valid | Stock and price go stale, so a claim needs an end time |
| Product | `product.product_id`, `product.gtin`, `product.name` | What is being offered | The claim must be about one exact product |
| Price | `price.unit_price`, `price.currency`, `price.vat_basis`, `price.per`, `price.pack_size` | The price and what it is a price of | A number without currency, VAT basis and unit cannot be compared or held to |
| Stock | `stock.available_quantity`, `stock.earliest_delivery` | How much can be ordered now and when it arrives | Needed to plan without a phone call |
| Origin | `origin.country`, `origin.producer`, `origin.certificates[]` (each with `type`, `id`, `issuer`) | Where it comes from and which certificates back that | A certificate ID can be checked with the body that issued it |

Rules:
- Token header: `alg` is `EdDSA`, `kid` is the key ID.
- Money is a decimal string (`"3.82"`), never a float.
- `exp - iat` comes from one config value, `offer_ttl_seconds`, default `600`. **Use real time here, not the demo clock from the reorder model.**
- **Origin fields come only from what the Valio mock data actually contains.** If the data has no origin for a product, set the field to `null`. Never invent a country, producer or certificate.

Example payload (values are illustrative):

```json
{
  "iss": "valio-aimo-gateway",
  "aud": "ravintola-kallio",
  "jti": "off_7f3a…",
  "iat": 1794650000,
  "exp": 1794650600,
  "product": { "product_id": "<Valio product number>", "gtin": null, "name": "Valio butter 500 g salted" },
  "price":   { "unit_price": "3.82", "currency": "EUR", "vat_basis": "excl", "per": "piece", "pack_size": 1 },
  "stock":   { "available_quantity": 1200, "earliest_delivery": "2026-11-16" },
  "origin":  { "country": null, "producer": null, "certificates": [] }
}
```

The MCP tools must return the signed token next to the readable fields, so an outside agent gets the same evidence Ukko does.

## 6. Part 3: the checks (buyer side)

Write one pure function (no network, no global state) that takes the token, the published keys, the current time and the buyer's settings, and returns a list of named checks, each with pass or fail and a plain-English reason.

| # | Check | Passes when | If it fails |
|---|---|---|---|
| 1 | Signature | The token verifies against the published key with the same `kid`. Accept `EdDSA` only: reject `none`, `HS256` and anything else. | Refuse the offer |
| 2 | Freshness | `exp` is in the future, and `iat` is not more than 60 seconds in the future | Refuse, then fetch a fresh offer |
| 3 | Supplier | `iss` is on the buyer's approved supplier list (config) | Refuse the offer |
| 4 | Audience | `aud` is this buyer | Refuse the offer |
| 5 | Order matches offer | The order line has the same product, unit price and currency; the quantity is at most `available_quantity`; the delivery date is not before `earliest_delivery`; and the order quotes the offer's `jti` and the SHA-256 hash of the token | Do not order; go back to planning |
| 6 | Not reused | This `jti` has not already been used for a placed order | Refuse, then fetch a fresh offer |
| 7 | Origin | If the request has an origin or certificate requirement, the signed origin fields state it | A person must approve ("origin not stated") |

Where the checks run:
- **At lookup:** checks 1 to 4 on every offer. An offer that fails is dropped and never reaches planning.
- **Immediately before placing an order, including after a person approves:** all seven. If the offer has expired, fetch a fresh one. If any signed field changed (price, quantity, delivery date), go back to planning and say so in the audit trail.
- Fetch the published keys once and cache them. If a token has an unknown `kid`, fetch the keys again once before refusing.

Policy additions (these are the only policy changes allowed):
- A failed check 1, 3 or 4 **blocks** the offer. A person cannot approve around a bad signature.
- A failed check 7 means **needs approval**, with the reason shown.

Reference for the core of checks 1 to 4. This runs correctly with PyJWT 2.15 and `cryptography`; adapt names to the project and confirm against the installed version:

```python
import json, jwt
from jwt.algorithms import OKPAlgorithm

def verify_signed_offer(token: str, jwks: dict, issuer: str, audience: str) -> dict:
    """Return the offer's contents if the token is genuine and fresh; raise otherwise."""
    kid = jwt.get_unverified_header(token).get("kid")
    matches = [k for k in jwks["keys"] if k.get("kid") == kid]
    if not matches:
        raise jwt.InvalidTokenError("unknown signing key")
    public_key = OKPAlgorithm.from_jwk(json.dumps(matches[0]))
    return jwt.decode(
        token, public_key,
        algorithms=["EdDSA"],                 # never accept anything else
        issuer=issuer, audience=audience,
        options={"require": ["exp", "iat", "iss", "aud", "jti"]},
    )
```

## 7. Part 4: the linked chain

- **Order:** quotes `offer_id` (the `jti`) and `offer_hash` (SHA-256 of the token), plus the ID of the buyer's signed mandate (the existing AP2 limits or approval).
- **Gateway, when it receives an order:** refuses it if the offer ID is unknown, expired or already used, or if the order line differs from the signed offer. This is what makes the signed price a commitment until expiry.
- **Receipt:** a signed token from the gateway with `order_id`, `offer_id`, `offer_hash`, the mandate ID, the total and `iat`.
- **Buyer, when it gets the receipt:** verifies the signature with the same published keys and confirms the offer hash and mandate ID are the ones it sent. Only then show "receipts verified".

Keep whatever the existing receipt and AP2 code already does. Add the missing links; do not rebuild it.

## 8. What the dashboard must show

**A "Verification" section** in order details and on the approval card:
- Signed by (issuer and key ID), offer ID, issued and expiry times.
- "Covered by the signature: price, stock, delivery date, origin."
- One line per check with a tick or a cross and the plain-English reason.
- Origin: country, producer and certificates, or "Origin not stated by supplier".
- The chain as four linked IDs: mandate, offer, order, receipt.
- A collapsed "Show signed offer" expander with the raw token and its decoded contents.

**A "Trust test" tab in Settings** with one button per scenario. Each takes a real, freshly signed offer, breaks it in one way, and runs it through **the same verification function the pipeline uses**. It never places an order.

| Scenario | How the offer is broken | Expected result |
|---|---|---|
| Price changed | Edit the price in the payload without re-signing | Refused: the signature does not match the contents |
| Expired | Signed with an expiry in the past | Refused: the offer has expired |
| Unknown signer | Signed with a different key | Refused: not signed by an approved supplier's key |
| Reused | An offer ID that already has a placed order | Refused: this offer was already used |
| Wrong buyer | `aud` is another kitchen | Refused: the offer was made for someone else |

Show the result on screen and write it to the audit trail, marked clearly as a test.

## 9. Audit trail wording

Plain language, with the real values:
- Lookup: "Got 14 signed offers from Valio. All 14 signatures match Valio's published key (valio-aimo-2026-11). Valid until 13:59."
- Refusal: "Refused an offer for Valio butter 500 g salted: the signature does not match its contents, so the price or another field was changed after signing. It was not used."
- Before ordering: "Checked the offer again before ordering: signature valid, 6 minutes left, order matches the offer."
- Origin: "The buyer asked for Finnish origin. The supplier's signed offer does not state an origin, so a person must approve."
- Receipt: "Receipt verified: signed by Valio's key and quotes the same offer and approval."

Do not write one entry per offer at lookup. One summary line, plus one line for each refusal.

## 10. Tests

- A valid offer passes checks 1 to 4.
- Each of these is refused: price changed after signing; expired; issued in the future; unknown `kid`; right `kid` but signed with a different key; wrong audience; issuer not on the approved list; `alg` of `none`; `HS256` with a shared secret.
- An order whose price differs from its offer is refused by the buyer check and by the gateway.
- A reused offer ID is refused by the gateway.
- A receipt quoting a different offer hash fails the chain check.
- Origin required but `null` in the offer gives "needs approval".
- An expired offer at ordering time triggers a fresh lookup; a changed price sends the request back to planning.

## 11. `TRUST.md`

Write a one-page `TRUST.md` in the project root and link it from the README. Describe only what is actually implemented. Contents:
1. The purpose, in three sentences (use section 2).
2. The minimum data standard: the field table from section 5.
3. The verification rules: the table from section 6.
4. **What a pass proves and what it does not:** it proves who made the claim and that it is unaltered and fresh. It does not prove the claim is true. Origin and certificate claims are trusted only as far as the certificate ID can be checked with the issuing body.
5. The open standards used (section 3).
6. Path to production: the key served from the supplier's own domain over HTTPS, key replacement using `kid`, certificates delivered as attestations signed by the certifying body (for example W3C Verifiable Credentials), and withdrawal of offers.
7. Limits of this demo: the gateway is our own mock, keys are generated locally, and origin data is only what the mock catalogue contains.

## 12. Steps

Do one step at a time. After each: summarise what changed in plain English, give the exact commands to test it, and wait for "continue".

- **T0. Read and report (no code changes).** Tell me:
  - how offers and receipts are signed today: which algorithm, whether it is a public/private key pair or a shared secret, where the keys live, and which fields the signature covers;
  - where verification happens, and whether expiry is checked;
  - how an order refers to an offer today;
  - **exactly which origin, producer, certificate and GTIN fields the Valio mock data contains**, with one real example;
  - what the MCP tools return;
  - where each part of this spec will go, and anything that conflicts with it.
  If today's signing already uses a public/private key pair in a different format, ask me before converting it.
- **T1.** The gateway key pair and `GET /.well-known/jwks.json`.
- **T2.** The signed offer with the fields in section 5.
- **T3.** The verification function and the tests in section 10 that need only it.
- **T4.** Run the checks at lookup and before ordering; add the two policy rules and the audit entries.
- **T5.** The "Verification" section on the dashboard.
- **T6.** The "Trust test" tab.
- **T7.** `TRUST.md` and the README link.
- **T8.** The linked chain: the gateway's checks on incoming orders and the receipt fields in section 7.

If time runs out, stop after T7. T1 to T7 are what the judges will see.

## 13. Working rules

- Do not change the reorder model, the spending rules other than the two additions in section 6, payments, or the Exa web checks.
- Do not invent file names, API fields or data: use what T0 finds.
- At most one new dependency (PyJWT), and only if the project has no signing library already.
- Never display or log a private key. The public key and the signed tokens are safe to show.
- Keep functions short and the verification function pure.
- When something fails, show the exact error and the smallest fix.
- If any instruction here conflicts with how the code already works, stop and ask.

## 14. Stretch (only after T8)

For an offer that states a certificate, use the existing Exa web check to look for that certificate ID or producer on the issuing body's own site, and show "independently confirmed" or "not confirmed" with the source next to the origin line.
