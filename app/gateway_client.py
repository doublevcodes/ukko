"""Stockpot's only door to the supplier: HTTP calls to the Valio agent gateway.

Stockpot uses the same public interface as any outside agent would. It never
calls Valio's own API directly.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from ap2.sdk.jwt_helper import verify_jwt
from jwcrypto.jwk import JWK

from app import categories, settings
from app.models import Offer

TIMEOUT_SECONDS = 25  # one gateway call can mean two Valio calls with a retry each
_client: Any = None
_supplier_key: JWK | None = None


class GatewayError(Exception):
    """The gateway (or Valio behind it) could not be reached or gave a bad answer."""


class GatewayRefused(Exception):
    """The gateway understood the request and said no. `code` says why."""

    def __init__(self, status: int, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


def use_client(client: Any) -> None:
    """Swap the HTTP client. Tests pass a client wired straight to the gateway app."""
    global _client, _supplier_key
    _client, _supplier_key = client, None


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


def supplier_key() -> JWK:
    """The supplier's public key, fetched once, for checking its signatures."""
    global _supplier_key
    if _supplier_key is None:
        _supplier_key = JWK(**_call("GET", "/.well-known/jwks.json")["keys"][0])
    return _supplier_key


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


def _to_offer(raw: dict[str, Any]) -> Offer:
    """Check the supplier's signature on an offer, then read it into our contract."""
    try:
        signed = verify_jwt(raw["offer_jwt"], supplier_key())
    except Exception as error:  # noqa: BLE001  any failure here means the offer cannot be trusted
        raise GatewayError("An offer from the supplier failed its signature check.") from error
    if signed.get("offer_id") != raw.get("offer_id") or signed.get("unit_price") != raw.get("unit_price"):
        raise GatewayError("An offer from the supplier does not match its own signature.")
    return Offer(
        **{key: raw.get(key) for key in (
            "product_id", "unit_price", "pack_size", "pack_unit", "available_quantity", "earliest_delivery",
            "offer_id", "expires_at", "offer_jwt", "comparison_price", "not_orderable_reason", "order_cutoff",
            "brand", "delivery_reliability", "co2_kg_per_kg", "kg_per_sales_unit",
        )},
        name=raw.get("name") or f"Product {raw['product_id']}",
        attributes=raw.get("claims") or [], labels=raw.get("labels") or [],
        sales_unit=raw.get("sales_unit") or "", orderable=bool(raw.get("orderable")),
        category=raw.get("category") or "", category_name=categories.name_of(raw.get("category")),
        listed_substitutes=raw.get("listed_substitutes") or [],
    )


def get_offers(product_ids: list[str], needed_by: date) -> list[Offer]:
    """Live, signed price and availability for a short list of products."""
    if not product_ids:
        return []
    body = _call("POST", "/offers", json={"product_ids": product_ids, "needed_by": needed_by.isoformat()})
    return [_to_offer(raw) for raw in body["offers"]]


def register_buyer(public_jwk: dict[str, Any]) -> None:
    _call("POST", "/buyers", json={"buyer_id": settings.BUYER_ID, "public_jwk": public_jwk})


def create_checkout(request_id: str, needed_by: date, lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Ask for a signed checkout at live prices. May answer {"existing_order": ...}."""
    body = {"buyer_id": settings.BUYER_ID, "request_id": request_id, "needed_by": needed_by.isoformat(), "lines": lines}
    result = _call("POST", "/checkouts", json=body)
    if "checkout_jwt" in result:
        try:
            verify_jwt(result["checkout_jwt"], supplier_key())
        except Exception as error:  # noqa: BLE001
            raise GatewayError("The checkout from the supplier failed its signature check.") from error
    return result


def complete_checkout(checkout_id: str, checkout_mandate: str, payment_mandate: str) -> dict[str, Any]:
    tokens = {"checkout_mandate": checkout_mandate, "payment_mandate": payment_mandate}
    return _call("POST", f"/checkouts/{checkout_id}/complete", json=tokens)
