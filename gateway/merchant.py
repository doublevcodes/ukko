"""The mock merchant: turns a cart into a signed checkout, then into an order.

Valio's mock API has no ordering endpoint, so ordering is simulated here.
The authorisation is real: checkouts and receipts are signed, and an order is
recorded only when the buyer presents valid AP2 mandates. No money moves.
"""

from __future__ import annotations

import secrets
import time
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ap2.sdk.generated.checkout_receipt import CheckoutReceipt, CheckoutReceiptError
from ap2.sdk.generated.payment_receipt import PaymentReceipt, PaymentReceiptSuccess
from ap2.sdk.generated.types.checkout import Checkout
from ap2.sdk.receipt_wrapper import ReceiptClient
from ap2.sdk.utils import compute_sha256_b64url

from gateway import keys, ledger, mandate_checks, offers, settings
from gateway.ledger import HELSINKI

_checkouts: dict[str, dict[str, Any]] = {}
_receipts = ReceiptClient()
PAYMENT_PROCESSOR = f"{settings.MERCHANT_ID}/mock-payment-processor"


class CheckoutError(Exception):
    """A checkout step was refused. `status` is the HTTP status to answer with."""

    def __init__(self, status: int, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


def _cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _stock_problems(live: list[dict], quantities: dict[str, int]) -> list[dict[str, Any]]:
    """Lines that cannot be supplied as asked, with the reason for each."""
    problems = []
    for offer in live:
        wanted, stock = quantities[offer["product_id"]], offer.get("available_quantity")
        if not offer["orderable"]:
            problems.append({"product_id": offer["product_id"], "requested": wanted, "available": 0,
                             "reason": offer["not_orderable_reason"]})
        elif stock is not None and stock < wanted:
            problems.append({"product_id": offer["product_id"], "requested": wanted, "available": stock,
                             "reason": f"Only {stock} in stock."})
    return problems


def _checkout_payload(checkout_id: str, live: list[dict], quantities: dict[str, int], extra: dict) -> dict[str, Any]:
    """Build the checkout in the UCP shape that AP2 expects, amounts in cents."""
    items, total = [], 0
    for number, offer in enumerate(live, start=1):
        price, quantity = _cents(Decimal(offer["unit_price"])), quantities[offer["product_id"]]
        amount = price * quantity
        total += amount
        items.append({
            "id": f"line-{number}",
            "item": {"id": offer["product_id"], "title": offer["name"], "price": price},
            "quantity": quantity,
            "totals": [{"type": "subtotal", "amount": amount}, {"type": "total", "amount": amount}],
        })
    note = "Prices exclude VAT. Invoiced on account."
    return {
        "id": checkout_id,
        "merchant": {"id": settings.MERCHANT_ID, "name": settings.MERCHANT_NAME},
        "line_items": items,
        "status": "ready_for_complete",
        "currency": settings.CURRENCY,
        "totals": [{"type": "subtotal", "amount": total}, {"type": "total", "amount": total, "display_text": note}],
        "links": [],
        "delivery_dates": {offer["product_id"]: offer["earliest_delivery"] for offer in live},
        "sales_units": {offer["product_id"]: offer["sales_unit"] for offer in live},
        **extra,
    }


def create_checkout(buyer_id: str, request_id: str, needed_by: date, lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Re-price a cart live and return a supplier-signed checkout.

    If this request was already ordered, the existing order is returned
    instead, so a retry can never create a second order.
    """
    existing = ledger.find_order(buyer_id, request_id)
    if existing:
        return {"existing_order": existing}
    if ledger.buyer_key(buyer_id) is None:
        raise CheckoutError(403, "unknown_buyer", f"Buyer '{buyer_id}' has not registered an approval key.")
    quantities = {line["product_id"]: line["quantity"] for line in lines}
    if len(quantities) != len(lines):
        raise CheckoutError(422, "duplicate_lines", "Each product may appear on one line only.")

    live = offers.get_offers(list(quantities), needed_by)["offers"]
    problems = _stock_problems(live, quantities)
    if problems:
        raise CheckoutError(409, "cannot_fulfil", "Some lines cannot be supplied as requested.", problems)

    checkout_id = f"chk_{secrets.token_hex(8)}"
    now = datetime.now(HELSINKI).replace(microsecond=0)
    expires = now + timedelta(seconds=settings.CHECKOUT_TTL_SECONDS)
    extra = {"buyer_id": buyer_id, "request_id": request_id, "needed_by": needed_by.isoformat(),
             "expires_at": expires.isoformat(), "iss": settings.MERCHANT_ID,
             "iat": int(now.timestamp()), "exp": int(expires.timestamp())}
    payload = _checkout_payload(checkout_id, live, quantities, extra)
    Checkout.model_validate(payload)  # fail loudly here rather than at the buyer
    checkout_jwt = keys.sign(payload)
    _checkouts[checkout_id] = {
        "id": checkout_id, "jwt": checkout_jwt, "hash": compute_sha256_b64url(checkout_jwt),
        "payload": payload, "buyer_id": buyer_id, "request_id": request_id,
        "total_cents": payload["totals"][1]["amount"], "expires": expires.timestamp(),
    }
    return {"checkout_id": checkout_id, "checkout_jwt": checkout_jwt, "checkout": payload,
            "audience": settings.MERCHANT_ID, "nonce": checkout_id, "expires_at": expires.isoformat()}


def _error_receipt(reference: str, problems: list[str]) -> str:
    receipt = CheckoutReceipt(root=CheckoutReceiptError(
        status="Error", iss=settings.MERCHANT_ID, iat=int(time.time()), reference=reference,
        error="mandate_rejected", error_description=" ".join(problems),
    ))
    return keys.sign(receipt.model_dump(mode="json", exclude_none=True))


def _order_lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [{
        "product_id": line["item"]["id"], "name": line["item"]["title"], "quantity": line["quantity"],
        "sales_unit": payload["sales_units"][line["item"]["id"]],
        "unit_price": f"{line['item']['price'] / 100:.2f}", "line_total": f"{line['totals'][1]['amount'] / 100:.2f}",
        "delivery_date": payload["delivery_dates"][line["item"]["id"]],
    } for line in payload["line_items"]]


def complete_checkout(checkout_id: str, checkout_mandate: str, payment_mandate: str) -> dict[str, Any]:
    """Verify the buyer's AP2 mandates and, if they hold, record the order."""
    checkout = _checkouts.get(checkout_id)
    if checkout is None:
        raise CheckoutError(404, "unknown_checkout", "No such checkout. It may be from before a gateway restart.")
    existing = ledger.find_order(checkout["buyer_id"], checkout["request_id"])
    if existing:
        return existing | {"duplicate": True}
    if time.time() > checkout["expires"]:
        raise CheckoutError(410, "checkout_expired", "This checkout has expired. Create a new one for current prices.")

    root = ledger.buyer_key(checkout["buyer_id"])
    flow, problems = mandate_checks.verify(checkout, root, checkout_mandate, payment_mandate)
    reference = mandate_checks.closed_reference(checkout_mandate)
    if problems:
        detail = {"violations": problems, "flow": flow, "receipt_jwt": _error_receipt(reference, problems)}
        raise CheckoutError(403, "mandate_rejected", "The mandates do not authorise this order.", detail)

    payload, chained = checkout["payload"], flow == "human_not_present"
    order = {
        "status": "confirmed", "buyer_id": checkout["buyer_id"], "request_id": checkout["request_id"],
        "checkout_id": checkout_id, "placed_at": datetime.now(HELSINKI).isoformat(timespec="seconds"),
        "currency": settings.CURRENCY, "total": f"{checkout['total_cents'] / 100:.2f}",
        "total_cents": checkout["total_cents"], "lines": _order_lines(payload), "flow": flow,
        "mandate_reference": reference, "payment_method": "Invoice on account (demo, no money moves)",
        "standing_mandate": mandate_checks.mandate_digest(payment_mandate) if chained else None,
    }
    return ledger.record_order(order, lambda order_id: _success_receipts(order_id, reference, payment_mandate))


def _success_receipts(order_id: str, reference: str, payment_mandate: str) -> dict[str, str]:
    """Signed AP2 receipts for the order: one for the checkout, one for the payment."""
    checkout_receipt = _receipts.create_checkout_receipt(settings.MERCHANT_ID, reference, order_id)
    # No real payment happens: the confirmation ids below are placeholders from the mock processor.
    payment_id = f"mockpay_{secrets.token_hex(6)}"
    payment_receipt = PaymentReceipt(root=PaymentReceiptSuccess(
        status="Success", iss=PAYMENT_PROCESSOR, iat=int(time.time()),
        reference=mandate_checks.closed_reference(payment_mandate),
        payment_id=payment_id, psp_confirmation_id=payment_id, network_confirmation_id=payment_id,
    ))
    return {
        "checkout_receipt_jwt": keys.sign(checkout_receipt.model_dump(mode="json", exclude_none=True)),
        "payment_receipt_jwt": keys.sign(payment_receipt.model_dump(mode="json", exclude_none=True)),
    }


def reset() -> None:
    _checkouts.clear()
