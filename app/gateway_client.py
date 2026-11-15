"""Ukko's only door to the supplier: HTTP calls to the Valio agent gateway.

Ukko uses the same public interface as any outside agent would. It never
calls Valio's own API directly.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

import httpx
from ap2.sdk.jwt_helper import verify_jwt
from jwcrypto.jwk import JWK

from app import categories, settings, trust, trust_config
from app.models import Offer, OrderRequest
from app.trust import TrustCheck

TIMEOUT_SECONDS = 25  # one gateway call can mean two Valio calls with a retry each
_client: Any = None
_supplier_jwks: dict[str, Any] | None = None


class GatewayError(Exception):
    """The gateway (or Valio behind it) could not be reached or gave a bad answer."""


class GatewayRefused(Exception):
    """The gateway understood the request and said no. `code` says why."""

    def __init__(self, status: int, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


def use_client(client: Any) -> None:
    """Swap the HTTP client. Tests pass a client wired straight to the gateway app."""
    global _client, _supplier_jwks
    _client, _supplier_jwks = client, None


def _http() -> Any:
    global _client
    if _client is None:
        _client = httpx.Client(base_url=settings.GATEWAY_URL, timeout=TIMEOUT_SECONDS)
    return _client


def _call(method: str, path: str, **kwargs: Any) -> Any:
    """One request. Sorts failures into "could not reach" and "was refused"."""
    try:
        response = _http().request(method, path, headers={"X-Agent-Id": settings.AGENT_ID}, **kwargs)
    except httpx.HTTPError as error:
        raise GatewayError(f"The supplier gateway could not be reached ({type(error).__name__}).") from error
    try:
        body = response.json()
    except ValueError as error:
        raise GatewayError(f"The supplier gateway gave an unreadable answer (HTTP {response.status_code}).") from error
    if response.status_code >= 500:
        raise GatewayError(body.get("message") or f"The supplier gateway failed (HTTP {response.status_code}).")
    if response.status_code >= 400:
        message = body.get("message") or str(body.get("detail") or "The supplier refused the request.")
        raise GatewayRefused(response.status_code, body.get("error", "refused"), message, body.get("detail"))
    return body


def health() -> dict[str, Any]:
    return _call("GET", "/health")


def fetch_jwks(*, force: bool = False) -> dict[str, Any]:
    """The supplier's published keys. Cached; refreshed once on an unknown kid."""
    global _supplier_jwks
    if _supplier_jwks is None or force:
        _supplier_jwks = _call("GET", "/.well-known/jwks.json")
    return _supplier_jwks


def supplier_key() -> JWK:
    """The first published supplier key (AP2 receipt checks still want a JWK)."""
    keys = fetch_jwks().get("keys") or []
    if not keys:
        raise GatewayError("The supplier published no keys.")
    return JWK(**{k: v for k, v in keys[0].items() if k != "d"})


def vocabulary() -> dict[str, Any]:
    return _call("GET", "/vocabulary")


def search_products(query: str, claims: list[str], labels: list[str], limit: int = 20) -> list[dict[str, Any]]:
    """Catalogue search. Claims and labels are hard filters. No prices: ask for offers."""
    params = {"q": query, "claims": claims, "labels": labels, "limit": limit}
    return _call("GET", "/products/search", params=params)["results"]


def get_product(product_id: str) -> dict[str, Any] | None:
    """Catalogue facts for one product, or None when the supplier does not list it."""
    try:
        return _call("GET", f"/products/{product_id}")
    except GatewayRefused as refusal:
        if refusal.status == 404:
            return None
        raise


def _checks_as_dicts(checks: list[TrustCheck]) -> list[dict[str, Any]]:
    return [{"name": c.name, "passed": c.passed, "reason": c.reason} for c in checks]


def _to_offer(raw: dict[str, Any], request: OrderRequest | None = None) -> Offer | None:
    """Verify the supplier's signature (checks 1–4). Return None when the offer is refused."""
    token = raw.get("offer_jwt") or ""
    jwks = fetch_jwks()
    settings_trust = trust_config.for_request(request)
    checks = trust.verify_offer(token, jwks, int(time.time()), settings_trust)
    if not trust.lookup_ok(checks):
        # Unknown kid: refresh JWKS once, then refuse if still unknown.
        header_kid = None
        try:
            header_kid = trust.unverified_header(token).get("kid")
        except Exception:  # noqa: BLE001
            pass
        if header_kid and all(k.get("kid") != header_kid for k in (jwks.get("keys") or [])):
            jwks = fetch_jwks(force=True)
            checks = trust.verify_offer(token, jwks, int(time.time()), settings_trust)
        if not trust.lookup_ok(checks):
            return None

    claims = trust.unverified_payload(token) if token else {}
    # Prefer signed claim values for the fields the protocol covers.
    product = claims.get("product") or {}
    price = claims.get("price") or {}
    stock = claims.get("stock") or {}
    origin = claims.get("origin") or {}
    unit_price = price.get("unit_price", raw.get("unit_price"))
    earliest = stock.get("earliest_delivery") or raw.get("earliest_delivery")
    fields = {key: raw.get(key) for key in (
        "pack_size", "pack_unit", "comparison_price", "not_orderable_reason", "order_cutoff",
        "brand", "delivery_reliability", "co2_kg_per_kg", "kg_per_sales_unit", "pieces_per_sales_unit",
    ) if raw.get(key) is not None}
    expires_at = raw.get("expires_at")
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    return Offer(
        **fields,
        product_id=product.get("product_id") or raw["product_id"],
        name=product.get("name") or raw.get("name") or f"Product {raw['product_id']}",
        unit_price=unit_price,
        currency=price.get("currency") or raw.get("currency") or "EUR",
        available_quantity=stock.get("available_quantity", raw.get("available_quantity")),
        earliest_delivery=date.fromisoformat(earliest) if earliest else None,
        attributes=raw.get("claims") or [], labels=raw.get("labels") or [],
        offer_id=claims.get("jti") or raw.get("offer_id") or "",
        expires_at=expires_at,
        offer_jwt=token,
        offer_hash=raw.get("offer_hash") or trust.token_hash(token),
        offer_claims=claims,
        trust_checks=_checks_as_dicts(checks),
        gtin=product.get("gtin"),
        origin_country=origin.get("country"),
        origin_producer=origin.get("producer"),
        origin_certificates=list(origin.get("certificates") or []),
        sales_unit=raw.get("sales_unit") or "", orderable=bool(raw.get("orderable")),
        category=raw.get("category") or "", category_name=categories.name_of(raw.get("category")),
        listed_substitutes=raw.get("listed_substitutes") or [],
    )


def verify_raw_offer(
    token: str,
    *,
    request: OrderRequest | None = None,
    order_line: trust.OrderLineQuote | None = None,
    used_offer_ids: set[str] | None = None,
    now: int | None = None,
) -> list[TrustCheck]:
    """Run the same verification function the pipeline uses (for Trust tests and pre-order)."""
    jwks = fetch_jwks()
    checks = trust.verify_offer(
        token, jwks, int(time.time() if now is None else now), trust_config.for_request(request),
        order_line=order_line, used_offer_ids=used_offer_ids,
    )
    if any(c.name == "signature" and not c.passed and "unknown kid" in c.reason for c in checks):
        jwks = fetch_jwks(force=True)
        checks = trust.verify_offer(
            token, jwks, int(time.time() if now is None else now), trust_config.for_request(request),
            order_line=order_line, used_offer_ids=used_offer_ids,
        )
    return checks


def get_offers(product_ids: list[str], needed_by: date, request: OrderRequest | None = None) -> tuple[list[Offer], list[dict[str, Any]]]:
    """Live, signed price and availability. Returns (accepted offers, refusal records)."""
    if not product_ids:
        return [], []
    body = _call("POST", "/offers", json={
        "product_ids": product_ids, "needed_by": needed_by.isoformat(), "buyer_id": settings.BUYER_ID,
    })
    accepted: list[Offer] = []
    refused: list[dict[str, Any]] = []
    for raw in body["offers"]:
        offer = _to_offer(raw, request)
        if offer is None:
            token = raw.get("offer_jwt") or ""
            checks = verify_raw_offer(token, request=request) if token else []
            refused.append({
                "product_id": raw.get("product_id"),
                "name": raw.get("name") or raw.get("product_id"),
                "checks": _checks_as_dicts(checks),
                "reason": next((c.reason for c in checks if not c.passed), "Offer refused."),
            })
            continue
        accepted.append(offer)
    return accepted, refused


def register_buyer(public_jwk: dict[str, Any]) -> None:
    _call("POST", "/buyers", json={"buyer_id": settings.BUYER_ID, "public_jwk": public_jwk})


def create_checkout(
    request_id: str, needed_by: date, lines: list[dict[str, Any]], mandate_id: str | None = None,
) -> dict[str, Any]:
    """Ask for a signed checkout at live prices. May answer {"existing_order": ...}."""
    body: dict[str, Any] = {
        "buyer_id": settings.BUYER_ID, "request_id": request_id,
        "needed_by": needed_by.isoformat(), "lines": lines,
    }
    if mandate_id:
        body["mandate_id"] = mandate_id
    result = _call("POST", "/checkouts", json=body)
    if "checkout_jwt" in result:
        try:
            verify_jwt(result["checkout_jwt"], supplier_key())
        except Exception as error:  # noqa: BLE001
            raise GatewayError("The checkout from the supplier failed its signature check.") from error
    return result


def complete_checkout(
    checkout_id: str, checkout_mandate: str, payment_mandate: str,
    stripe_payment_intent_id: str | None = None, payment_method_label: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"checkout_mandate": checkout_mandate, "payment_mandate": payment_mandate}
    if stripe_payment_intent_id:
        body["stripe_payment_intent_id"] = stripe_payment_intent_id
    if payment_method_label:
        body["payment_method_label"] = payment_method_label
    return _call("POST", f"/checkouts/{checkout_id}/complete", json=body)
