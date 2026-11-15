"""Gateway and buyer chain: offer quote, reuse, receipt hash."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from ap2.sdk.jwt_helper import verify_jwt

from app import authority, gateway_client, mandates, ordering, settings
from app.models import Offer, OrderRequest, Plan, PlanLine, PolicyConfig
from gateway import offer_store, offers
from tests.helpers import shelf_runs_low


def test_gateway_refuses_reused_offer_id(world):
    needed = date.today() + timedelta(days=2)
    batch = offers.get_offers(["1001"], needed, audience=settings.BUYER_ID)
    offer = batch["offers"][0]
    offer_store.mark_used(offer["offer_id"], "VA-ALREADY")
    authority.ensure(PolicyConfig())
    lines = [{
        "product_id": "1001", "quantity": 1,
        "offer_id": offer["offer_id"], "offer_hash": offer["offer_hash"],
    }]
    try:
        gateway_client.create_checkout("req-reuse", needed, lines)
        assert False, "expected refusal"
    except gateway_client.GatewayRefused as refused:
        assert refused.code == "offer_invalid"
        assert "already used" in refused.message.lower()


def test_gateway_refuses_price_mismatch_on_quoted_offer(world):
    needed = date.today() + timedelta(days=2)
    batch = offers.get_offers(["1001"], needed, audience=settings.BUYER_ID)
    offer = batch["offers"][0]
    record = offer_store.get(offer["offer_id"])
    assert record is not None
    record["claims"]["price"]["unit_price"] = "0.01"
    offer_store._offers[offer["offer_id"]] = record  # noqa: SLF001
    authority.ensure(PolicyConfig())
    lines = [{
        "product_id": "1001", "quantity": 1,
        "offer_id": offer["offer_id"], "offer_hash": offer["offer_hash"],
    }]
    try:
        gateway_client.create_checkout("req-price", needed, lines)
        assert False, "expected refusal"
    except gateway_client.GatewayRefused as refused:
        assert refused.code == "offer_invalid"


def test_receipt_quotes_offer_hash(world):
    needed = date.today() + timedelta(days=2)
    batch = offers.get_offers(["1002"], needed, audience=settings.BUYER_ID)
    raw = batch["offers"][0]
    offer = Offer(
        product_id=raw["product_id"], name=raw["name"], unit_price=Decimal(raw["unit_price"]),
        earliest_delivery=date.fromisoformat(raw["earliest_delivery"]), orderable=True,
        available_quantity=raw.get("available_quantity"), offer_id=raw["offer_id"],
        offer_jwt=raw["offer_jwt"], offer_hash=raw["offer_hash"], offer_claims=raw["offer_claims"],
        category=raw.get("category") or "", category_name="dairy", sales_unit="ST",
        trust_checks=[{"name": "signature", "passed": True, "reason": "ok"}],
    )
    plan = Plan(
        id="A", lines=[PlanLine(offer=offer, quantity=1, line_total=offer.unit_price)],
        total=offer.unit_price, meets_deadline=True, meets_constraints=True, coverage=Decimal(1),
        label="test", delivery_date=offer.earliest_delivery,
    )
    request = OrderRequest(
        id="req-hash", source="typed", item_description="milk", quantity=Decimal(1),
        unit="l", needed_by=needed, search_terms="milk",
    )
    confirmation = ordering.place(request, plan, PolicyConfig(), approved_by_person=False)
    assert confirmation.receipts_verified
    receipt = verify_jwt(confirmation.checkout_receipt_jwt, gateway_client.supplier_key())
    assert offer.offer_hash in (receipt.get("offer_hashes") or [receipt.get("offer_hash")])
    # A different expected hash must fail the chain check.
    assert not mandates.receipts_match(
        {
            "order_id": confirmation.order_id,
            "checkout_receipt_jwt": confirmation.checkout_receipt_jwt,
            "payment_receipt_jwt": confirmation.payment_receipt_jwt,
        },
        "not-a-mandate", "not-a-mandate", gateway_client.supplier_key(),
        expected_offer_hashes=["0" * 64],
    )


def test_pipeline_still_orders_with_trust_checks(world):
    record = shelf_runs_low()
    assert record.decision.status == "ordered"
    offer = record.decision.chosen_plan.lines[0].offer
    assert offer.offer_jwt
    assert offer.offer_claims.get("iss") == "valio-aimo-gateway"
    assert any(c["name"] == "signature" and c["passed"] for c in offer.trust_checks)
    assert record.decision.order_confirmation.receipts_verified
    assert record.decision.order_confirmation.offer_ids
