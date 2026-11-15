"""Remember signed offers the gateway has issued, so an order can quote one.

An offer id (jti) may be used for at most one placed order. The store is in
memory for the demo; a restart forgets unused offers (already-used ones are
also remembered via the order ledger).
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

_lock = threading.Lock()
_offers: dict[str, dict[str, Any]] = {}  # jti -> record


def token_hash(token: str) -> str:
    """SHA-256 hex digest of the compact JWT (the offer_hash an order quotes)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def remember(token: str, claims: dict[str, Any]) -> None:
    jti = claims["jti"]
    with _lock:
        _offers[jti] = {
            "token": token,
            "hash": token_hash(token),
            "claims": claims,
            "used_by_order": None,
        }


def get(jti: str) -> dict[str, Any] | None:
    with _lock:
        record = _offers.get(jti)
        return dict(record) if record else None


def mark_used(jti: str, order_id: str) -> None:
    with _lock:
        record = _offers.get(jti)
        if record is not None:
            record["used_by_order"] = order_id


def is_used(jti: str) -> bool:
    with _lock:
        record = _offers.get(jti)
        return bool(record and record.get("used_by_order"))


def used_jtis() -> set[str]:
    with _lock:
        return {jti for jti, record in _offers.items() if record.get("used_by_order")}


def reset() -> None:
    with _lock:
        _offers.clear()


def problems_for_line(
    offer_id: str,
    offer_hash: str,
    product_id: str,
    unit_price: str,
    currency: str,
    quantity: int,
    delivery_date: str | None,
    now: float | None = None,
) -> list[str]:
    """Plain-English reasons this order line cannot use the quoted offer."""
    now = time.time() if now is None else now
    record = get(offer_id)
    if record is None:
        return [f"Offer {offer_id} is unknown (it may be from before a gateway restart)."]
    if record["hash"] != offer_hash:
        return [f"Offer {offer_id}: the offer hash does not match the signed token."]
    if record.get("used_by_order"):
        return [f"Offer {offer_id} was already used for order {record['used_by_order']}."]
    claims = record["claims"]
    if claims.get("exp", 0) <= now:
        return [f"Offer {offer_id} has expired."]
    product = claims.get("product") or {}
    price = claims.get("price") or {}
    stock = claims.get("stock") or {}
    problems = []
    if product.get("product_id") != product_id:
        problems.append(f"Offer {offer_id} is for a different product.")
    if str(price.get("unit_price")) != str(unit_price) or price.get("currency") != currency:
        problems.append(f"Offer {offer_id}: the order price does not match the signed offer.")
    available = stock.get("available_quantity")
    if available is not None and quantity > int(available):
        problems.append(f"Offer {offer_id}: asked for {quantity}, only {available} available.")
    earliest = stock.get("earliest_delivery")
    if delivery_date and earliest and delivery_date < earliest:
        problems.append(f"Offer {offer_id}: delivery {delivery_date} is before earliest {earliest}.")
    return problems
