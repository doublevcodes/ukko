"""Work out how many litres or kilos one sales unit of a product contains.

Valio's unit codes (KI, ST, RAS...) are not translated in the data, so the
content cannot be read from them. Instead we divide the unit price by the
API's per-litre or per-kilo comparison price, and cross-check the result
against the size written in the product name or in the catalogue.
"""

from __future__ import annotations

import re
from decimal import Decimal

# Sizes such as "1 l", "5 dl", "2,5 dl", "400g", "100x2 cl", "90 x 30g".
_SIZE = re.compile(r"(?:(\d+)\s*x\s*)?(\d+(?:[.,]\d+)?)\s*(kg|g|l|dl|cl|ml)\b", re.IGNORECASE)
_TO_BASE = {
    "kg": (Decimal(1), "KG"), "g": (Decimal("0.001"), "KG"),
    "l": (Decimal(1), "L"), "dl": (Decimal("0.1"), "L"),
    "cl": (Decimal("0.01"), "L"), "ml": (Decimal("0.001"), "L"),
}
# The comparison price is rounded to cents, so the division is slightly off.
TOLERANCE = Decimal("0.06")


def size_from_name(name: str, wanted_unit: str) -> Decimal | None:
    """Read the size of one base unit from a product name, in L or KG.

    Returns None when the name has no size in the wanted unit.
    """
    found = None
    for count, amount, unit in _SIZE.findall(name):
        factor, kind = _TO_BASE[unit.lower()]
        if kind != wanted_unit:
            continue
        found = Decimal(amount.replace(",", ".")) * factor * Decimal(count or 1)
    return found


def _agrees(candidate: Decimal, ratio: Decimal) -> bool:
    return abs(candidate - ratio) <= ratio * TOLERANCE


def _plain(value: Decimal) -> Decimal:
    """Drop trailing zeros without switching to scientific notation (10, not 1E+1)."""
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value.normalize()


def pack_content(
    name: str,
    base_units_per_sales_unit: Decimal,
    catalogue_size: Decimal | None,
    unit_price: Decimal,
    comparison_price: Decimal | None,
    comparison_unit: str | None,
) -> tuple[Decimal | None, str]:
    """Return (litres or kilos per sales unit, how we know).

    `catalogue_size` is the size of one base unit from the catalogue's weights
    and dimensions, already in the comparison unit, if the catalogue has it.
    The content is None when it cannot be trusted; the second value says why.
    """
    if comparison_unit not in ("L", "KG") or not comparison_price or comparison_price <= 0 or unit_price <= 0:
        return None, "no comparison price"
    ratio = unit_price / comparison_price

    candidates = []
    named = size_from_name(name, comparison_unit)
    if named:
        candidates.append((named * base_units_per_sales_unit, "product name"))
    if catalogue_size:
        candidates.append((catalogue_size * base_units_per_sales_unit, "catalogue dimensions"))

    for candidate, source in candidates:
        if _agrees(candidate, ratio):
            return _plain(candidate), f"{source}, confirmed by price"
    if candidates:
        seen = ", ".join(f"{_plain(value)} from {source}" for value, source in candidates)
        return None, f"pack size unclear: {seen}, but the price implies {ratio:.2f}"
    return ratio.quantize(Decimal("0.01")), "price divided by comparison price"
