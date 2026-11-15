"""Pipeline step 7, the ordering part: re-check, sign, place the order.

Asking the supplier for a checkout re-prices the cart live. That is the
"re-check immediately before ordering": if anything moved since the plan was
made, no order is placed and the pipeline plans again.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app import authority, gateway_client, mandates, payments, settings, trust
from app.gateway_client import GatewayRefused
from app.models import OrderConfirmation, OrderRequest, Plan, PolicyConfig
from app.payments import PaymentError


class Replan(Exception):
    """Price, stock or delivery changed since the plan was made. The message says what."""


class SupplierWantsAPerson(Exception):
    """The supplier's own check refused the agent's authority for this order."""


class PaymentFailed(Exception):
    """Stripe could not charge the kitchen card. Nothing was ordered at the supplier."""


class TrustBlocked(Exception):
    """A trust check that cannot be approved around failed just before ordering."""


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


def _pre_order_trust(plan: Plan, request: OrderRequest) -> tuple[list[dict[str, Any]], list[str]]:
    """Run all seven trust checks on each line immediately before ordering."""
    used: set[str] = set()
    all_checks: list[dict[str, Any]] = []
    problems: list[str] = []
    for line in plan.lines:
        offer = line.offer
        if not offer.offer_jwt:
            problems.append(f"{offer.name}: missing signed offer.")
            continue
        quote = trust.OrderLineQuote(
            product_id=offer.product_id,
            unit_price=trust.money_str(offer.unit_price),
            currency=offer.currency,
            quantity=line.quantity,
            delivery_date=trust.delivery_iso(offer.earliest_delivery),
            offer_id=offer.offer_id,
            offer_hash=offer.offer_hash or trust.token_hash(offer.offer_jwt),
        )
        checks = gateway_client.verify_raw_offer(
            offer.offer_jwt, request=request, order_line=quote, used_offer_ids=used,
        )
        all_checks.extend({"product_id": offer.product_id, **c.__dict__} for c in checks)
        if trust.blocks_offer(checks):
            problems.append(next(c.reason for c in checks if not c.passed and c.name in {"signature", "supplier", "audience"}))
            continue
        freshness = next((c for c in checks if c.name == "freshness"), None)
        if freshness and not freshness.passed:
            raise Replan("The signed offer expired before ordering; fetching a fresh one.")
        match = next((c for c in checks if c.name == "order_matches_offer"), None)
        if match and not match.passed:
            raise Replan(f"A signed field changed since planning: {match.reason}")
        reused = next((c for c in checks if c.name == "not_reused"), None)
        if reused and not reused.passed:
            raise Replan(reused.reason)
        # Origin failure is handled by policy when planning; at order time after person
        # approval it is allowed through.
    return all_checks, problems


def _confirmation(order: dict[str, Any], receipts_verified: bool, trust_checks: list[dict[str, Any]]) -> OrderConfirmation:
    return OrderConfirmation(
        order_id=order["order_id"], total=Decimal(order["total"]), currency=order.get("currency", "EUR"),
        placed_at=datetime.fromisoformat(order["placed_at"]), lines=order.get("lines", []),
        flow=order["flow"], mandate_reference=order["mandate_reference"],
        checkout_receipt_jwt=order["checkout_receipt_jwt"], payment_receipt_jwt=order["payment_receipt_jwt"],
        receipts_verified=receipts_verified, payment_method=order.get("payment_method", ""),
        offer_ids=list(order.get("offer_ids") or []),
        offer_hashes=list(order.get("offer_hashes") or []),
        mandate_id=order.get("mandate_id") or order.get("mandate_reference") or "",
        trust_checks=trust_checks,
    )


def place(request: OrderRequest, plan: Plan, policy: PolicyConfig, approved_by_person: bool) -> OrderConfirmation:
    """Place the order for `plan`, or raise Replan / SupplierWantsAPerson / a gateway error.

    The same request id never creates two orders: the supplier answers a
    repeat with the order it already has.
    """
    trust_checks, block_problems = _pre_order_trust(plan, request)
    if block_problems:
        raise TrustBlocked(" ".join(block_problems))

    standing = authority.ensure(policy)
    mandate_id = standing.open_checkout[:32] if standing else ""
    lines = [{
        "product_id": line.offer.product_id,
        "quantity": line.quantity,
        "offer_id": line.offer.offer_id,
        "offer_hash": line.offer.offer_hash or trust.token_hash(line.offer.offer_jwt),
    } for line in plan.lines]
    try:
        checkout = gateway_client.create_checkout(request.id, request.needed_by, lines, mandate_id=mandate_id)
    except GatewayRefused as refusal:
        if refusal.code == "cannot_fulfil":
            raise Replan(_stock_changes(refusal.detail)) from refusal
        if refusal.code in {"offer_invalid", "offer_expired"}:
            raise Replan(refusal.message) from refusal
        raise
    if "existing_order" in checkout:
        return _confirmation(checkout["existing_order"], receipts_verified=False, trust_checks=trust_checks)
    changes = changes_since_plan(plan, checkout["checkout"])
    if changes:
        raise Replan(" ".join(changes))
    # Quote matched live prices; still compare delivery dates for the worse.

    minutes_left = None
    for line in plan.lines:
        if line.offer.expires_at:
            left = int(line.offer.expires_at.timestamp()) - int(time.time())
            minutes_left = left // 60 if minutes_left is None else min(minutes_left, left // 60)
    # Audit-friendly summary is written by the pipeline from trust_checks.

    supplier = authority.supplier()
    if approved_by_person:
        tokens = mandates.sign_as_manager(authority.manager_key(), checkout, supplier, settings.BUYER_ID)
    else:
        tokens = mandates.sign_as_agent(authority.agent_key(), standing, checkout, supplier, settings.BUYER_ID)

    # Charge the saved kitchen card before the supplier records the order.
    total_cents = checkout["checkout"]["totals"][1]["amount"]
    total = Decimal(total_cents) / 100
    currency = checkout["checkout"].get("currency") or "EUR"
    try:
        payment_intent_id, payment_label = payments.charge(
            total, currency, request.id, f"Valio Aimo order for request {request.id}",
        )
    except PaymentError as error:
        raise PaymentFailed(str(error)) from error

    try:
        order = gateway_client.complete_checkout(
            checkout["checkout_id"], *tokens,
            stripe_payment_intent_id=payment_intent_id, payment_method_label=payment_label,
        )
    except GatewayRefused as refusal:
        if refusal.code == "mandate_rejected" and not approved_by_person:
            violations = (refusal.detail or {}).get("violations") or [refusal.message]
            raise SupplierWantsAPerson(" ".join(str(v) for v in violations)) from refusal
        if refusal.code == "checkout_expired":
            raise Replan("The supplier's checkout expired before the order was placed.") from refusal
        if refusal.code in {"offer_invalid", "offer_expired"}:
            raise Replan(refusal.message) from refusal
        raise
    verified = mandates.receipts_match(
        order, *tokens, gateway_client.supplier_key(),
        expected_offer_hashes=[line.offer.offer_hash or trust.token_hash(line.offer.offer_jwt) for line in plan.lines],
        expected_mandate_id=mandate_id or None,
    )
    return _confirmation(order, verified, trust_checks)
