"""Live offers: price and availability for a short list of products, signed.

An offer is the supplier's answer to "what would this cost and when could I
have it?". Each one is signed and carries an expiry, so an agent can prove
later what it was told. The JWT payload is the minimum trust-protocol claim
set (identity, freshness, product, price, stock, origin).
"""

from __future__ import annotations

import math
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from gateway import catalogue, keys, offer_store, settings, units, valio_client


def read_availability(entry: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Turn one Valio availability entry into plain facts.

    `available_quantity` is None when stock is not limited (or not known, for
    products delivered straight from the supplier's warehouse).
    """
    facts: dict[str, Any] = {
        "orderable": False, "not_orderable_reason": None, "available_quantity": None,
        "earliest_delivery": None, "order_cutoff": None, "stock_model": None,
    }
    if entry is None:
        return facts | {"not_orderable_reason": "Valio gave no availability answer for this product."}
    facts["stock_model"] = entry.get("logisticModel")
    lines = entry.get("scheduleLines") or []
    if entry.get("salesBlock") or (lines and lines[0].get("salesBlock")):
        return facts | {"not_orderable_reason": "This product is blocked for sale right now."}
    if not lines or not lines[0].get("availableDate"):
        return facts | {"not_orderable_reason": "This product cannot currently be delivered on any date."}
    amount = lines[0].get("availableAmount") or 0
    if amount <= 0:
        return facts | {"not_orderable_reason": "This product is out of stock."}
    facts["earliest_delivery"] = lines[0]["availableDate"][:10]
    if amount < settings.UNLIMITED_STOCK_FROM:
        facts["available_quantity"] = math.floor(amount)
    cutoff = datetime.fromisoformat(entry["closingTime"]) if entry.get("closingTime") else None
    facts["order_cutoff"] = cutoff.isoformat() if cutoff else None
    if settings.ENFORCE_ORDER_CUTOFF and cutoff and now > cutoff:
        reason = (
            f"The order cut-off for delivery on {facts['earliest_delivery']} passed on "
            f"{cutoff:%d %b at %H:%M}, and Valio's API gives no later delivery date."
        )
        return facts | {"not_orderable_reason": reason}
    return facts | {"orderable": True}


def _money(price: dict[str, Any] | None) -> Decimal | None:
    return Decimal(str(price["value"])) if price and price.get("value") is not None else None


def _build_offer(product_id: str, availability: dict | None, price: dict | None, now: datetime) -> dict[str, Any]:
    """Combine catalogue facts, live availability and live price into one offer."""
    product = catalogue.get(product_id)
    if product is None:
        return {"product_id": product_id, "orderable": False,
                "not_orderable_reason": "This product is not in the catalogue."}
    offer = product.model_dump(exclude={"id", "name_fi", "base_units_per_sales_unit", "base_unit_litres", "base_unit_kg"})
    offer = {"product_id": product.id, **offer, **read_availability(availability, now)}
    pieces = max(Decimal(1), Decimal(str(product.base_units_per_sales_unit or 1)))
    offer |= {"currency": settings.CURRENCY, "unit_price": None, "pack_size": None, "pack_unit": None,
              "comparison_price": None, "vat_rate": None, "kg_per_sales_unit": None,
              "pieces_per_sales_unit": str(pieces)}

    unit_price = None if not price or price.get("error") else _money(price.get("catalogPrice"))
    if unit_price is None or unit_price <= 0:
        return offer | {"orderable": False, "not_orderable_reason": "Valio has no price for this product."}
    comparison, unit = _money(price.get("comparisonPrice")), price.get("comparisonUnit")
    per_base = product.base_unit_litres if unit == "L" else product.base_unit_kg
    content, how = units.pack_content(
        product.name, Decimal(str(product.base_units_per_sales_unit)),
        Decimal(str(per_base)) if per_base else None, unit_price, comparison, unit,
    )
    weight = content if unit == "KG" else None
    if weight is None and product.base_unit_kg:
        weight = Decimal(str(product.base_unit_kg)) * Decimal(str(product.base_units_per_sales_unit))
    return offer | {
        "unit_price": str(unit_price),
        "comparison_price": str(comparison) if comparison else None,
        "pack_size": str(content) if content else None,
        "pack_unit": unit if content else None,
        "pack_size_source": how,
        "vat_rate": (price.get("tax") or {}).get("taxRate"),
        "kg_per_sales_unit": str(weight) if weight else None,
    }


def _origin_block(offer: dict[str, Any]) -> dict[str, Any]:
    """Origin and certificates only from catalogue data — never invented."""
    labels = offer.get("labels") or []
    certificates = [
        {"type": "packagingMarkedLabel", "id": label, "issuer": "GS1 Synkka"}
        for label in labels
    ]
    return {
        "country": offer.get("country_of_origin"),
        "producer": offer.get("producer"),
        "certificates": certificates,
    }


def signed_claims(offer: dict[str, Any], audience: str, issued: datetime, expires: datetime, offer_id: str) -> dict[str, Any]:
    """The minimum trust-protocol payload covered by the supplier's signature."""
    pieces = offer.get("pieces_per_sales_unit") or "1"
    try:
        pack_size = int(Decimal(str(pieces)))
    except Exception:  # noqa: BLE001
        pack_size = 1
    return {
        "iss": settings.MERCHANT_ID,
        "aud": audience,
        "jti": offer_id,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "product": {
            "product_id": offer["product_id"],
            "gtin": offer.get("gtin"),
            "name": offer.get("name"),
        },
        "price": {
            "unit_price": offer.get("unit_price"),
            "currency": offer.get("currency") or settings.CURRENCY,
            "vat_basis": "excl",
            "per": "piece",
            "pack_size": pack_size,
        },
        "stock": {
            "available_quantity": offer.get("available_quantity"),
            "earliest_delivery": offer.get("earliest_delivery"),
        },
        "origin": _origin_block(offer),
    }


def get_offers(product_ids: list[str], needed_by: date, audience: str | None = None) -> dict[str, Any]:
    """Ask Valio for live price and availability, and return signed offers.

    Prices exclude VAT. Raises ValioError when Valio cannot be reached.
    `audience` is the buyer the offer is made for (JWT `aud`).
    """
    audience = audience or settings.DEFAULT_OFFER_AUDIENCE
    availability = valio_client.availability(product_ids, needed_by)
    prices = valio_client.price(product_ids, needed_by)
    # Valio's clock decides whether a cut-off has passed; wall clock dates the signatures.
    valio_now = datetime.fromisoformat(availability["timeNow"])
    by_id_availability = {entry["productId"]: entry for entry in availability["products"]}
    by_id_price = {entry["productId"]: entry for entry in prices["products"]}
    issued = datetime.now().astimezone().replace(microsecond=0)
    if valio_now.tzinfo is not None and issued.tzinfo is None:
        issued = issued.replace(tzinfo=valio_now.tzinfo)
    expires = issued + timedelta(seconds=settings.OFFER_TTL_SECONDS)

    offers = []
    for product_id in product_ids:
        offer = _build_offer(product_id, by_id_availability.get(product_id), by_id_price.get(product_id), valio_now)
        offer_id = f"off_{secrets.token_hex(8)}"
        claims = signed_claims(offer, audience, issued, expires, offer_id)
        token = keys.sign(claims)
        offer_store.remember(token, claims)
        offer |= {
            "offer_id": offer_id, "needed_by": needed_by.isoformat(),
            "issuer": settings.MERCHANT_ID, "issued_at": issued.isoformat(), "expires_at": expires.isoformat(),
            "audience": audience, "offer_hash": offer_store.token_hash(token),
            "offer_claims": claims, "offer_jwt": token,
        }
        offers.append(offer)
    return {"server_time": valio_now.isoformat(), "offers": offers}
