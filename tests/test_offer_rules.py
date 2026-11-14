"""Piece vs sales-unit conversion: 10 cans must not become 10 wholesale 24-packs."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app import offer_rules
from app.models import Offer, OrderRequest


def _offer(**changes) -> Offer:
    base = dict(
        product_id="477389", name="NOCCO BCAA Arancia 24x330ml", unit_price=Decimal("51.12"),
        earliest_delivery=date.today() + timedelta(days=2), orderable=True,
        sales_unit="KI", pieces_per_sales_unit=Decimal(24),
    )
    return Offer(**(base | changes))


def _request(quantity: int = 10, unit: str = "pcs") -> OrderRequest:
    return OrderRequest(
        id="req-test", source="typed", item_description="NOCCO", quantity=Decimal(quantity),
        unit=unit, needed_by=date.today() + timedelta(days=1), search_terms="nocco",
    )


def test_ten_cans_need_one_twenty_four_pack():
    offer = _offer()
    request = _request(10, "pcs")
    assert offer_rules.per_sales_unit(offer, request) == Decimal(24)
    assert offer_rules.units_for(offer, request, request.quantity) == 1


def test_twenty_four_cans_need_one_pack():
    offer = _offer()
    request = _request(24, "pcs")
    assert offer_rules.units_for(offer, request, request.quantity) == 1


def test_twenty_five_cans_round_up_to_two_packs():
    offer = _offer()
    request = _request(25, "pcs")
    assert offer_rules.units_for(offer, request, request.quantity) == 2


def test_single_can_sales_unit_stays_one_to_one():
    offer = _offer(pieces_per_sales_unit=Decimal(1), sales_unit="ST", name="Monster Energy can 0,5 L")
    request = _request(10, "pcs")
    assert offer_rules.units_for(offer, request, request.quantity) == 10
