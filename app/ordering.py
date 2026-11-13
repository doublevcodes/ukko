"""Pipeline step 7, the ordering part: re-check, sign, place the order.

Asking the supplier for a checkout re-prices the cart live. That is the
"re-check immediately before ordering": if anything moved since the plan was
made, no order is placed and the pipeline plans again.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app import authority, gateway_client, mandates, settings
from app.gateway_client import GatewayRefused
from app.models import OrderConfirmation, OrderRequest, Plan, PolicyConfig


class Replan(Exception):
    """Price, stock or delivery changed since the plan was made. The message says what."""


class SupplierWantsAPerson(Exception):
    """The supplier's own check refused the agent's authority for this order."""


def _stock_changes(problems: Any) -> str:
    """Turn the supplier's "cannot fulfil" answer into a sentence."""
    if not isinstance(problems, list) or not problems:
        return "The supplier can no longer supply the plan as it stands."
    parts = [f"product {p.get('product_id')}: wanted {p.get('requested')}, {p.get('available')} available ({p.get('reason')})"
             for p in problems]
    return "Stock changed since the plan was made: " + "; ".join(parts)


def changes_since_plan(plan: Plan, checkout: dict[str, Any]) -> list[str]:
    """Compare the live checkout with the plan. Only changes for the worse are returned."""
    live_price = {item["item"]["id"]: Decimal(item["item"]["price"]) / 100 for item in checkout["line_items"]}
    changes = []
    for line in plan.lines:
        offer = line.offer
        price = live_price.get(offer.product_id)
        if price is not None and price > offer.unit_price:
            changes.append(f"The price of {offer.name} rose from {offer.unit_price:.2f} to {price:.2f} EUR.")
        delivery = (checkout.get("delivery_dates") or {}).get(offer.product_id)
        if delivery and date.fromisoformat(delivery) > offer.earliest_delivery:
            changes.append(f"Delivery of {offer.name} moved from {offer.earliest_delivery} to {delivery}.")
    return changes


def _confirmation(order: dict[str, Any], receipts_verified: bool) -> OrderConfirmation:
    return OrderConfirmation(
        order_id=order["order_id"], total=Decimal(order["total"]), currency=order.get("currency", "EUR"),
        placed_at=datetime.fromisoformat(order["placed_at"]), lines=order.get("lines", []),
        flow=order["flow"], mandate_reference=order["mandate_reference"],
        checkout_receipt_jwt=order["checkout_receipt_jwt"], payment_receipt_jwt=order["payment_receipt_jwt"],
        receipts_verified=receipts_verified, payment_method=order.get("payment_method", ""),
    )


def place(request: OrderRequest, plan: Plan, policy: PolicyConfig, approved_by_person: bool) -> OrderConfirmation:
    """Place the order for `plan`, or raise Replan / SupplierWantsAPerson / a gateway error.

    The same request id never creates two orders: the supplier answers a
    repeat with the order it already has.
    """
    standing = authority.ensure(policy)
    lines = [{"product_id": line.offer.product_id, "quantity": line.quantity} for line in plan.lines]
    try:
        checkout = gateway_client.create_checkout(request.id, request.needed_by, lines)
    except GatewayRefused as refusal:
        if refusal.code == "cannot_fulfil":
            raise Replan(_stock_changes(refusal.detail)) from refusal
        raise
    if "existing_order" in checkout:
        return _confirmation(checkout["existing_order"], receipts_verified=False)
    changes = changes_since_plan(plan, checkout["checkout"])
    if changes:
        raise Replan(" ".join(changes))

    supplier = authority.supplier()
    if approved_by_person:
        tokens = mandates.sign_as_manager(authority.manager_key(), checkout, supplier, settings.BUYER_ID)
    else:
        tokens = mandates.sign_as_agent(authority.agent_key(), standing, checkout, supplier, settings.BUYER_ID)
    try:
        order = gateway_client.complete_checkout(checkout["checkout_id"], *tokens)
    except GatewayRefused as refusal:
        if refusal.code == "mandate_rejected" and not approved_by_person:
            violations = (refusal.detail or {}).get("violations") or [refusal.message]
            raise SupplierWantsAPerson(" ".join(str(v) for v in violations)) from refusal
        if refusal.code == "checkout_expired":
            raise Replan("The supplier's checkout expired before the order was placed.") from refusal
        raise
    verified = mandates.receipts_match(order, *tokens, gateway_client.supplier_key())
    return _confirmation(order, verified)
