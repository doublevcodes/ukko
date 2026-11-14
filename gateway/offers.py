"""Live offers: price and availability for a short list of products, signed.

An offer is the supplier's answer to "what would this cost and when could I
have it?". Each one is signed and carries an expiry, so an agent can prove
later what it was told.
"""

from __future__ import annotations

import math
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from gateway import catalogue, keys, settings, units, valio_client


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


def get_offers(product_ids: list[str], needed_by: date) -> dict[str, Any]:
    """Ask Valio for live price and availability, and return signed offers.

    Prices exclude VAT. Raises ValioError when Valio cannot be reached.
    """
    availability = valio_client.availability(product_ids, needed_by)
    prices = valio_client.price(product_ids, needed_by)
    # Valio's clock decides whether a cut-off has passed; our own clock dates the signatures.
    valio_now = datetime.fromisoformat(availability["timeNow"])
    by_id_availability = {entry["productId"]: entry for entry in availability["products"]}
    by_id_price = {entry["productId"]: entry for entry in prices["products"]}
    issued = datetime.now(valio_now.tzinfo).replace(microsecond=0)
    expires = issued + timedelta(seconds=settings.OFFER_TTL_SECONDS)

    offers = []
    for product_id in product_ids:
        offer = _build_offer(product_id, by_id_availability.get(product_id), by_id_price.get(product_id), valio_now)
        offer |= {
            "offer_id": f"off_{secrets.token_hex(8)}", "needed_by": needed_by.isoformat(),
            "issuer": settings.MERCHANT_ID, "issued_at": issued.isoformat(), "expires_at": expires.isoformat(),
        }
        claims = {"iss": settings.MERCHANT_ID, "iat": int(issued.timestamp()), "exp": int(expires.timestamp())}
        offers.append(offer | {"offer_jwt": keys.sign(offer | claims)})
    return {"server_time": valio_now.isoformat(), "offers": offers}
