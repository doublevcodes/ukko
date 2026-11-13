"""Verify the AP2 mandates a buyer sends to complete a checkout.

Two flows are accepted, both from AP2 v0.2:
- Human present: the buyer's manager key signed this exact checkout and
  payment. Each mandate is a single signed token.
- Human not present: the manager key signed standing limits ("open"
  mandates) and the agent's key signed this checkout under them. Each
  mandate is a chain: open mandate, then the agent's closed mandate.

Every function returns a list of problems in plain words; empty means fine.
"""

from __future__ import annotations

import time
from typing import Any

from ap2.sdk.checkout_mandate_chain import CheckoutMandateChain
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.mandate import MandateClient
from ap2.sdk.payment_mandate_chain import PaymentMandateChain
from ap2.sdk.utils import compute_sha256_b64url
from jwcrypto.jwk import JWK

from gateway import ledger, settings

_client = MandateClient()
CHAIN_SEPARATOR = "~~"


def mandate_digest(token: str) -> str:
    """Identify a mandate by the hash of its first signed part.

    For a chain this is the open mandate. AP2's payment.reference constraint
    needs "the digest of the open checkout mandate" without fixing the exact
    recipe, so buyer and supplier here both use this one.
    """
    return compute_sha256_b64url(token.split("~", 1)[0])


def closed_reference(token: str) -> str:
    """The hash a receipt uses to point at the mandate that authorised it."""
    return compute_sha256_b64url(_client.get_closed_mandate_jwt(token))


def _expired(mandates: list[Any]) -> list[str]:
    now = time.time()
    return [
        f"A mandate ({mandate.vct}) expired before it was presented."
        for mandate in mandates if mandate.exp is not None and mandate.exp < now
    ]


def _closed_problems(checkout: dict[str, Any], closed: CheckoutMandate, payment: PaymentMandate) -> list[str]:
    """Check that the mandates authorise exactly this checkout and its total."""
    problems = _expired([closed, payment])
    if closed.checkout_jwt != checkout["jwt"] or closed.checkout_hash != checkout["hash"]:
        problems.append("The checkout mandate is for a different checkout.")
    if payment.transaction_id != checkout["hash"]:
        problems.append("The payment mandate is for a different checkout.")
    if payment.payee.id != settings.MERCHANT_ID:
        problems.append(f"The payment mandate pays '{payment.payee.id}', not this supplier.")
    amount = payment.payment_amount
    if amount.currency != settings.CURRENCY or amount.amount != checkout["total_cents"]:
        problems.append(
            f"The payment mandate authorises {amount.amount / 100:.2f} {amount.currency}, "
            f"but the checkout total is {checkout['total_cents'] / 100:.2f} {settings.CURRENCY}."
        )
    return problems


def _verify_direct(checkout: dict, root: JWK, checkout_token: str, payment_token: str) -> list[str]:
    closed = _client.verify(token=checkout_token, key_or_provider=root, payload_type=CheckoutMandate)
    payment = _client.verify(token=payment_token, key_or_provider=root, payload_type=PaymentMandate)
    return _closed_problems(checkout, closed.mandate_payload, payment.mandate_payload)


def _verify_chain(checkout: dict, root: JWK, checkout_token: str, payment_token: str) -> list[str]:
    def provider(_token: Any) -> JWK:
        return root  # the manager's key is the root of trust for the whole chain

    # The nonce is the checkout id we issued, so a chain cannot be replayed on another checkout.
    expected = {"expected_aud": settings.MERCHANT_ID, "expected_nonce": checkout["id"]}
    checkout_chain = CheckoutMandateChain.parse(_client.verify(token=checkout_token, key_or_provider=provider, **expected))
    payment_chain = PaymentMandateChain.parse(_client.verify(token=payment_token, key_or_provider=provider, **expected))

    problems = _closed_problems(checkout, checkout_chain.closed_mandate, payment_chain.closed_mandate)
    problems += _expired([checkout_chain.open_mandate, payment_chain.open_mandate])
    problems += checkout_chain.verify(expected_checkout_hash=checkout["hash"], checkout_jwt=checkout["jwt"])
    problems += payment_chain.verify(
        expected_transaction_id=checkout["hash"],
        expected_open_checkout_hash=mandate_digest(checkout_token),
        mandate_context=ledger.usage(mandate_digest(payment_token)),
    )
    return problems


def verify(checkout: dict[str, Any], root: JWK, checkout_token: str, payment_token: str) -> tuple[str, list[str]]:
    """Verify both mandates. Returns (flow name, problems)."""
    chained = CHAIN_SEPARATOR in checkout_token
    flow = "human_not_present" if chained else "human_present"
    if chained != (CHAIN_SEPARATOR in payment_token):
        return flow, ["The checkout and payment mandates must use the same flow."]
    try:
        check = _verify_chain if chained else _verify_direct
        return flow, check(checkout, root, checkout_token, payment_token)
    except Exception as error:  # noqa: BLE001  any failure to verify means "not authorised"
        return flow, [f"The mandates could not be verified ({type(error).__name__}: {error})."]
