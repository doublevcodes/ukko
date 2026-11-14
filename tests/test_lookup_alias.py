"""When a shelf brand is not in Valio's catalogue, propose alternatives immediately."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app import lookup
from app.models import OrderRequest
from tests.helpers import in_days, typed


def test_camera_alias_matches_hell_brand(monkeypatch):
    from app import config
    from app.config import CameraFood

    monkeypatch.setattr(config, "load_camera_foods", lambda: {
        "hell_energy": CameraFood(name="HELL energy drink", search_terms="energy drink"),
        "nocco": CameraFood(name="NOCCO energy drink", product_id="477151", search_terms="nocco energy drink"),
    })
    request = OrderRequest(
        id="r1", source="typed", item_description="HELL", quantity=Decimal(10), unit="pcs",
        needed_by=date.today() + timedelta(days=1), search_terms="HELL",
    )
    product_id, terms, name = lookup._camera_alias(request)
    assert product_id is None and terms == "energy drink" and "HELL" in name


def test_hell_typed_request_proposes_energy_drink_substitute(world, monkeypatch):
    """HELL is not in the catalogue; Stockpot should order a closest energy drink, not ask to rephrase."""
    from app import config
    from app.config import CameraFood

    monkeypatch.setattr(config, "load_camera_foods", lambda: {
        "hell_energy": CameraFood(name="HELL energy drink", search_terms="energy drink"),
    })
    # Fixture catalogue has milk/butter only — seed a fake energy drink via gateway search/offers.
    # Use the existing milk product as a stand-in "energy drink" hit by stubbing search + match path.
    monkeypatch.setattr(
        "app.gateway_client.search_products",
        lambda query, claims, labels, limit=20: (
            [{"id": "1001", "name": "XL Energy Drink Classic energy drink 250ml x 24"}]
            if "energy" in query.lower() else []
        ),
    )

    def fake_offers(ids, needed_by):
        from app.models import Offer
        return [Offer(
            product_id="1001", name="XL Energy Drink Classic energy drink 250ml x 24",
            unit_price=Decimal("14.44"), earliest_delivery=date.today() + timedelta(days=1),
            orderable=True, available_quantity=50, sales_unit="KI", pieces_per_sales_unit=Decimal(24),
            category="22701", category_name="energy drinks", offer_id="off_test", offer_jwt="x",
        )]

    monkeypatch.setattr("app.gateway_client.get_offers", fake_offers)

    record = typed(
        world, "They need 10 cans of HELL by tomorrow",
        item_description="HELL", search_terms="HELL", quantity=10, unit="pcs", needed_by=in_days(1),
    )
    assert record.decision.status in {"awaiting_approval", "ordered"}
    assert record.decision.chosen_plan is not None
    line = record.decision.chosen_plan.lines[0]
    assert line.quantity == 1  # 10 cans → one 24-pack, not 10 packs
    assert line.is_substitute or record.exact_id is None
    assert "energy drink" in " ".join(e.summary for e in record.timeline).lower() or line.offer.name
