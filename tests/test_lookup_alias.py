"""When a shelf brand is not in Valio's catalogue, propose alternatives immediately."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app import lookup, planner
from app.models import Offer, OrderRequest
from tests.helpers import in_days, typed


def test_choose_exact_prefers_plain_milk_not_cheapest_coffee_milk():
    """Lowest price must not make coffee milk the exact item for a generic milk request."""
    request = OrderRequest(
        id="r1", source="typed", item_description="lactose-free milk", quantity=Decimal(50),
        unit="l", needed_by=date.today() + timedelta(days=3), search_terms="milk",
        constraints=["FREE_FROM_LACTOSE"],
    )
    coffee = Offer(
        product_id="9999", name="Valio coffee milk lactose free 1 L", unit_price=Decimal("0.50"),
        earliest_delivery=date.today() + timedelta(days=1), orderable=True, available_quantity=999,
        attributes=["FREE_FROM_LACTOSE"], pack_size=Decimal(1), pack_unit="L",
    )
    plain = Offer(
        product_id="1002", name="Valio semi skimmed milk 1 l lactose free", unit_price=Decimal("1.50"),
        earliest_delivery=date.today() + timedelta(days=1), orderable=True, available_quantity=999,
        attributes=["FREE_FROM_LACTOSE"], pack_size=Decimal(1), pack_unit="L",
    )
    assert planner.choose_exact(request, [coffee, plain]) == "1002"


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


def test_hell_typed_request_orders_closest_energy_drink_as_exact(world, monkeypatch):
    """HELL is not in the catalogue; closest energy drink is Plan A exact so Autopilot can order."""
    from app import config, state
    from app.config import CameraFood
    from app.models import PolicyConfig

    monkeypatch.setattr(config, "load_camera_foods", lambda: {
        "hell_energy": CameraFood(name="HELL energy drink", search_terms="energy drink"),
    })
    # Fixture PolicyConfig() only allows dairy; shelf demo needs energy drinks too.
    state.policy = PolicyConfig(allowed_categories=["dairy", "plant-based drinks", "energy drinks"])
    # Fixture catalogue has milk/butter only — seed a fake energy drink via gateway search/offers.
    monkeypatch.setattr(
        "app.gateway_client.search_products",
        lambda query, claims, labels, limit=20: (
            [{"id": "1001", "name": "XL Energy Drink Classic energy drink 250ml x 24"}]
            if "energy" in query.lower() else []
        ),
    )

    def fake_offers(ids, needed_by, request=None):
        # Real signed offer for 1001 (trust + checkout), dressed as an energy drink for planning.
        from app import settings as app_settings
        from app.gateway_client import _to_offer
        from gateway import offers as gateway_offers
        raw = gateway_offers.get_offers(["1001"], needed_by, audience=app_settings.BUYER_ID)["offers"][0]
        offer = _to_offer(raw, request)
        assert offer is not None
        offer.name = "XL Energy Drink Classic energy drink 250ml x 24"
        offer.category = "22701"
        offer.category_name = "energy drinks"
        offer.pieces_per_sales_unit = Decimal(24)
        offer.sales_unit = "KI"
        offer.available_quantity = 50
        return [offer], []

    monkeypatch.setattr("app.gateway_client.get_offers", fake_offers)

    record = typed(
        world, "They need 10 cans of HELL by tomorrow",
        item_description="HELL", search_terms="HELL", quantity=10, unit="pcs", needed_by=in_days(1),
    )
    assert record.decision.status == "awaiting_approval"
    assert record.decision.policy_result.outcome == "needs_approval"
    assert any("Manual request rule" in reason for reason in record.decision.policy_result.reasons)
    assert record.decision.chosen_plan is not None
    assert record.decision.chosen_plan.id == "A"
    line = record.decision.chosen_plan.lines[0]
    assert line.quantity == 1  # 10 cans → one 24-pack, not 10 packs
    assert not line.is_substitute
    assert record.exact_id == "1001"
    assert "closest catalogue match" in " ".join(e.summary for e in record.timeline).lower()
