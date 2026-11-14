"""Small questions about one offer: can it be used for this request, and how?

Plain code shared by the planner and the substitute ranking.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal

from app.models import Offer, OrderRequest

UNIT_WORDS = {"l": "litre", "kg": "kilo", "pcs": "piece"}
PACK_UNIT_FOR = {"l": "L", "kg": "KG"}
# "24x330ml", "24 x 330 ml", "250ml x 24" — count of cans/bottles in one sales unit.
_PACK_COUNT = re.compile(
    r"(?:(\d+)\s*[x×]\s*\d+(?:[.,]\d+)?\s*(?:ml|cl|dl|l|g|kg)\b)"
    r"|(?:\d+(?:[.,]\d+)?\s*(?:ml|cl|dl|l|g|kg)\s*[x×]\s*(\d+)\b)",
    re.IGNORECASE,
)


def pieces_from_name(name: str) -> Decimal | None:
    """How many pieces one sales unit holds, read from names like '24x330ml'."""
    match = _PACK_COUNT.search(name or "")
    if not match:
        return None
    count = match.group(1) or match.group(2)
    pieces = Decimal(count)
    return pieces if pieces > 1 else None


def pieces_per_unit(offer: Offer) -> Decimal:
    """Cans/bottles/items in one wholesaler sales unit.

    Prefer the supplier's figure. If it is missing or 1, fall back to the pack
    count in the product name so "12 cans" never becomes twelve 24-packs.
    """
    pieces = offer.pieces_per_sales_unit if offer.pieces_per_sales_unit > 0 else Decimal(1)
    if pieces > 1:
        return pieces
    return pieces_from_name(offer.name) or pieces


def per_sales_unit(offer: Offer, request: OrderRequest) -> Decimal | None:
    """How much of the request's unit one sales unit covers (None if it cannot be compared)."""
    if request.unit == "pcs":
        # Buyer counts cans/bottles/items; the wholesaler sells multi-packs (e.g. 24×330 ml).
        return pieces_per_unit(offer)
    if offer.pack_unit == PACK_UNIT_FOR[request.unit] and offer.pack_size and offer.pack_size > 0:
        return offer.pack_size
    return None


def price_per_unit(offer: Offer, request: OrderRequest) -> Decimal | None:
    """Price per litre, kilo or piece: the fair way to compare two products."""
    size = per_sales_unit(offer, request)
    if offer.unit_price is None or size is None:
        return None
    return (offer.unit_price / size).quantize(Decimal("0.0001"))


def missing_constraints(offer: Offer, request: OrderRequest) -> list[str]:
    facts = set(offer.attributes) | set(offer.labels)
    return [need for need in request.constraints if need not in facts]


def why_unusable(offer: Offer, request: OrderRequest) -> str | None:
    """The reason this offer cannot be part of any plan, or None if it can."""
    if not offer.orderable:
        return offer.not_orderable_reason or "It cannot be ordered right now."
    if offer.unit_price is None or offer.earliest_delivery is None:
        return "The supplier gave no price or delivery date."
    missing = missing_constraints(offer, request)
    if missing:
        return f"It is not marked {', '.join(missing)} in the catalogue."
    if per_sales_unit(offer, request) is None:
        return f"Its size per {UNIT_WORDS[request.unit]} could not be worked out reliably."
    price = price_per_unit(offer, request)
    if request.max_unit_price is not None and price is not None and price > request.max_unit_price:
        return (f"At {price:.2f} EUR per {UNIT_WORDS[request.unit]} it is above the stated "
                f"maximum of {request.max_unit_price:.2f} EUR.")
    if offer.available_quantity is not None and offer.available_quantity < 1:
        return "It is out of stock."
    return None


def on_time(offer: Offer, request: OrderRequest) -> bool:
    return offer.earliest_delivery is not None and offer.earliest_delivery <= request.needed_by


def units_for(offer: Offer, request: OrderRequest, amount: Decimal) -> int:
    """Whole sales units needed to cover `amount` of the request's unit (rounded up)."""
    return max(1, math.ceil(amount / per_sales_unit(offer, request)))


def units_in_stock(offer: Offer, wanted: int) -> int:
    """How many of the wanted sales units the supplier can deliver."""
    return wanted if offer.available_quantity is None else min(wanted, offer.available_quantity)


def preference_matches(offer: Offer, request: OrderRequest) -> int:
    facts = set(offer.attributes) | set(offer.labels)
    return sum(1 for wish in request.preferences if wish in facts)
