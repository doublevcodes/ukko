"""AP2 (Agent Payments Protocol v0.2), buyer side.

Two keys live here:
- the manager key, which stands for the kitchen manager's signing device;
- the agent key, which the agent uses on its own.

The manager key signs standing limits ("open mandates"): which supplier the
agent may order from, how much per order, and how much in total. Within those
limits the agent key signs each order by itself. Anything else needs the
manager key to sign that exact order, which happens when a person clicks
Approve. The supplier checks every signature before it accepts an order.

In this demo both keys sit in the Stockpot backend. In production the manager
key would live on the manager's phone or a hardware key (see README).
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from ap2.sdk.disclosure_metadata import DisclosureMetadata
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.open_checkout_mandate import AllowedMerchants, OpenCheckoutMandate
from ap2.sdk.generated.open_payment_mandate import (
    AgentRecurrence, AllowedPayees, AmountRange, Budget, Frequency, OpenPaymentMandate, PaymentReference,
)
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from ap2.sdk.jwt_helper import verify_jwt
from ap2.sdk.mandate import MandateClient
from ap2.sdk.sdjwt import common as sdjwt_common
from ap2.sdk.utils import compute_sha256_b64url
from jwcrypto.jwk import JWK
from pydantic import BaseModel

STANDING_VALID_SECONDS = 7 * 24 * 3600
ORDER_VALID_SECONDS = 15 * 60
_client = MandateClient()


class StandingLimits(BaseModel):
    """The agent's standing authority, signed by the manager key."""

    open_checkout: str
    open_payment: str
    max_order_eur: Decimal
    budget_eur: Decimal
    supplier_id: str
    issued_at: int
    expires_at: int


def load_or_create_key(path: Path, key_id: str) -> JWK:
    """Load a P-256 signing key from disk, creating it on first use."""
    if path.exists():
        return JWK(**json.loads(path.read_text(encoding="utf-8")))
    key = JWK.generate(kty="EC", crv="P-256", kid=key_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.export(), encoding="utf-8")
    path.chmod(0o600)  # private key: readable by the owner only
    return key


def public_jwk(key: JWK) -> dict[str, Any]:
    return json.loads(key.export_public())


def cents(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value())


def mandate_digest(token: str) -> str:
    """Identify a mandate by the hash of its first signed part (same recipe as the gateway)."""
    return compute_sha256_b64url(token.split("~", 1)[0])


def _sign_root(payload: BaseModel, issuer_key: JWK) -> str:
    """Do what MandateClient.create does, but with enums written as plain text.

    The pinned SDK dumps the mandate without JSON mode, so the recurrence
    frequency (an enum) cannot be signed. This uses the SDK's own building
    blocks with a JSON-mode dump; the resulting token is the same format.
    """
    claims = sdjwt_common.selectively_disclosable_claims(
        payload.model_dump(mode="json", by_alias=True, exclude_none=True),
        DisclosureMetadata.from_model(payload),
    )
    issuer = sdjwt_common.issue_sd_jwt(
        claims=claims, issuer_key=issuer_key, header_params=sdjwt_common.header_parameters(issuer_key),
        add_decoy_claims=False, serialization_format="compact",
    )
    return issuer.sd_jwt_issuance


def sign_standing_limits(manager: JWK, agent: JWK, supplier: Merchant, max_order: Decimal, budget: Decimal) -> StandingLimits:
    """Manager signs: "my agent may order from this supplier, within these limits"."""
    now = int(time.time())
    window = {"cnf": {"jwk": public_jwk(agent)}, "iat": now, "exp": now + STANDING_VALID_SECONDS}
    open_checkout = _client.create(
        payloads=[OpenCheckoutMandate(constraints=[AllowedMerchants(allowed=[supplier])], **window)],
        issuer_key=manager,
    )
    constraints = [
        AmountRange(currency="EUR", min=0, max=cents(max_order)),
        AllowedPayees(allowed=[supplier]),
        # Reusable until the total budget is spent; AP2 requires a budget with recurrence.
        AgentRecurrence(frequency=Frequency.ON_DEMAND),
        Budget(max=float(budget), currency="EUR"),
        # Ties this payment authority to the checkout authority above.
        PaymentReference(conditional_transaction_id=mandate_digest(open_checkout)),
    ]
    open_payment = _sign_root(OpenPaymentMandate(constraints=constraints, **window), manager)
    return StandingLimits(
        open_checkout=open_checkout, open_payment=open_payment, max_order_eur=max_order, budget_eur=budget,
        supplier_id=supplier.id, issued_at=now, expires_at=now + STANDING_VALID_SECONDS,
    )


def _closed_mandates(checkout: dict[str, Any], supplier: Merchant, buyer_id: str) -> tuple[CheckoutMandate, PaymentMandate]:
    """The two "this exact order" mandates, tied together by the checkout's hash."""
    now = int(time.time())
    window = {"iat": now, "exp": now + ORDER_VALID_SECONDS}
    checkout_hash = compute_sha256_b64url(checkout["checkout_jwt"])
    total = checkout["checkout"]["totals"][1]["amount"]
    invoice = PaymentInstrument(id=f"account:{buyer_id}", type="invoice_on_account",
                                description="Kitchen card on file (Stripe test); AP2 authorises the cart")
    return (
        CheckoutMandate(checkout_jwt=checkout["checkout_jwt"], checkout_hash=checkout_hash, **window),
        PaymentMandate(transaction_id=checkout_hash, payee=supplier, payment_instrument=invoice,
                       payment_amount=Amount(amount=total, currency=checkout["checkout"]["currency"]), **window),
    )


def sign_as_agent(agent: JWK, standing: StandingLimits, checkout: dict[str, Any], supplier: Merchant, buyer_id: str) -> tuple[str, str]:
    """Human not present: the agent signs this order under the standing limits."""
    closed_checkout, closed_payment = _closed_mandates(checkout, supplier, buyer_id)
    binding = {"holder_key": agent, "nonce": checkout["nonce"], "aud": checkout["audience"]}
    return (
        _client.present(mandate_token=standing.open_checkout, payloads=[closed_checkout], **binding),
        _client.present(mandate_token=standing.open_payment, payloads=[closed_payment], **binding),
    )


def sign_as_manager(manager: JWK, checkout: dict[str, Any], supplier: Merchant, buyer_id: str) -> tuple[str, str]:
    """Human present: the manager signs this exact order (called when a person approves)."""
    closed_checkout, closed_payment = _closed_mandates(checkout, supplier, buyer_id)
    return (
        _client.create(payloads=[closed_checkout], issuer_key=manager),
        _client.create(payloads=[closed_payment], issuer_key=manager),
    )


def receipts_match(
    order: dict[str, Any], checkout_mandate: str, payment_mandate: str, supplier_key: JWK,
    *, expected_offer_hashes: list[str] | None = None, expected_mandate_id: str | None = None,
) -> bool:
    """Check the supplier's signed receipts really refer to the mandates (and offer) we sent."""
    try:
        checkout_receipt = verify_jwt(order["checkout_receipt_jwt"], supplier_key)
        payment_receipt = verify_jwt(order["payment_receipt_jwt"], supplier_key)
    except Exception:  # noqa: BLE001  a bad signature or a missing receipt both mean "not verified"
        return False

    def reference(token: str) -> str:
        return compute_sha256_b64url(_client.get_closed_mandate_jwt(token))

    ok = (
        checkout_receipt.get("status") == "Success"
        and checkout_receipt.get("order_id") == order.get("order_id")
        and checkout_receipt.get("reference") == reference(checkout_mandate)
        and payment_receipt.get("status") == "Success"
        and payment_receipt.get("reference") == reference(payment_mandate)
    )
    if not ok:
        return False
    if expected_offer_hashes:
        receipt_hashes = checkout_receipt.get("offer_hashes") or (
            [checkout_receipt["offer_hash"]] if checkout_receipt.get("offer_hash") else []
        )
        if list(receipt_hashes) != list(expected_offer_hashes):
            return False
    if expected_mandate_id and checkout_receipt.get("mandate_id"):
        if checkout_receipt.get("mandate_id") != expected_mandate_id:
            return False
    return True
