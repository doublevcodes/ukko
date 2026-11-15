# Trust protocol (Ukko ↔ Valio Aimo gateway)

## Purpose

1. The supplier publishes a public key at a fixed address.
2. Every claim about price, stock and origin arrives as a signed offer with a short expiry time.
3. The agent orders on its own only if the signature is valid, the offer is fresh, and the order matches the offer exactly. Otherwise the offer is refused or a person decides.

A pass proves **who** made the claim and that it is **unaltered and fresh**. It does **not** prove the claim is true. Origin and certificate claims are trusted only as far as the certificate id can be checked with the issuing body.

## Minimum data standard (signed offer JWT)

| Group | Field | Meaning |
|---|---|---|
| Identity | `iss` | Who is making the claim (`valio-aimo-gateway`) |
| | `aud` | The buyer the offer was made for |
| | `jti` | Unique offer id |
| Freshness | `iat`, `exp` | Issued / expiry (wall clock; default TTL 600s) |
| Product | `product.product_id`, `product.gtin`, `product.name` | Exact product |
| Price | `price.unit_price`, `price.currency`, `price.vat_basis`, `price.per`, `price.pack_size` | Price as a decimal string, excl. VAT |
| Stock | `stock.available_quantity`, `stock.earliest_delivery` | How much / when |
| Origin | `origin.country`, `origin.producer`, `origin.certificates[]` | From catalogue data only; `null` / `[]` when unknown |

## Verification rules

| # | Check | If it fails |
|---|---|---|
| 1 | Signature (ES256 against published `kid`) | Refuse the offer |
| 2 | Freshness (`exp` future; `iat` not far in the future) | Refuse; fetch a fresh offer |
| 3 | Supplier (`iss` on approved list) | Refuse the offer |
| 4 | Audience (`aud` is this buyer) | Refuse the offer |
| 5 | Order matches offer (product, price, qty, delivery, `jti`, hash) | Do not order; re-plan |
| 6 | Not reused (`jti` unused) | Refuse; fetch a fresh offer |
| 7 | Origin (if the request requires it, signed fields state it) | Needs a person's approval |

Failed checks 1, 3 or 4 **block** the offer — a person cannot approve around a bad signature. Failed check 7 **needs approval**.

## Open standards used

- JWT (RFC 7519) as JWS (RFC 7515)
- Algorithm: **ES256** (P-256) — same as the AP2 SDK already used for mandates and receipts
- Public keys: JWK Set (RFC 7517) at `GET /.well-known/jwks.json`
- Product identity: GTIN when the catalogue has one, plus Valio product number

## Path to production

- Serve the key from the supplier's own domain over HTTPS
- Rotate keys with a new `kid` while keeping the old one published during overlap
- Deliver certificates as attestations signed by the certifying body (for example W3C Verifiable Credentials)
- Support withdrawal of offers before expiry

## Limits of this demo

- The gateway is our own mock in front of Valio's Junction API
- Keys are generated locally under `data/gateway/keys/` (gitignored)
- Origin data is only what the mock catalogue contains (`countryOfOrigin`, Synkka producer, packaging labels)
- Signing uses ES256/AP2 rather than EdDSA so mandates, offers and receipts share one stack
