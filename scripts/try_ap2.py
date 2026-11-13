"""Standalone check that the AP2 SDK works on this machine.

Runs both AP2 flows with throwaway keys and no network:
  1. Human not present: the manager signs standing limits, the agent signs
     one order under them, the supplier verifies the chain.
  2. Human present: the manager signs one exact order directly.
It also shows the supplier refusing an order above the standing limit.

Run:  python scripts/try_ap2.py
"""

from __future__ import annotations

import json
import time

from ap2.sdk.checkout_mandate_chain import CheckoutMandateChain
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.open_checkout_mandate import AllowedMerchants, OpenCheckoutMandate
from ap2.sdk.generated.open_payment_mandate import AllowedPayees, AmountRange, OpenPaymentMandate
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from ap2.sdk.jwt_helper import create_jwt, verify_jwt
from ap2.sdk.mandate import MandateClient
from ap2.sdk.payment_mandate_chain import PaymentMandateChain
from ap2.sdk.receipt_wrapper import ReceiptClient
from ap2.sdk.utils import compute_sha256_b64url
from jwcrypto.jwk import JWK

SUPPLIER = Merchant(id="valio-aimo", name="Valio Aimo (mock)")
INVOICE = PaymentInstrument(id="acct-demo", type="invoice_on_account", description="Invoice on account (demo)")
AUD = "valio-aimo"
client = MandateClient()


def new_key(kid: str) -> JWK:
    """Make a fresh P-256 signing key with a readable key id."""
    return JWK.generate(kty="EC", crv="P-256", kid=kid)


def public(key: JWK) -> JWK:
    return JWK(**json.loads(key.export_public()))


def signed_checkout(supplier_key: JWK, checkout_id: str, total_cents: int) -> str:
    """What the supplier would return: a signed description of one order."""
    line_totals = [{"type": "subtotal", "amount": total_cents}, {"type": "total", "amount": total_cents}]
    payload = {
        "id": checkout_id,
        "merchant": SUPPLIER.model_dump(exclude_none=True),
        "line_items": [{
            "id": "line-1",
            "item": {"id": "464201", "title": "Valio semi skimmed milk 1 l lactose free UHT", "price": 156},
            "quantity": total_cents // 156,
            "totals": line_totals,
        }],
        "status": "ready_for_complete",
        "currency": "EUR",
        "totals": line_totals,
        "links": [],
    }
    return create_jwt({"alg": "ES256", "typ": "JWT", "kid": supplier_key["kid"]}, payload, supplier_key)


def closed_mandates(checkout_jwt: str, total_cents: int) -> tuple[CheckoutMandate, PaymentMandate]:
    """The two 'this exact order' mandates, tied together by the checkout hash."""
    now = int(time.time())
    checkout_hash = compute_sha256_b64url(checkout_jwt)
    checkout = CheckoutMandate(checkout_jwt=checkout_jwt, checkout_hash=checkout_hash, iat=now, exp=now + 900)
    payment = PaymentMandate(
        transaction_id=checkout_hash, payee=SUPPLIER,
        payment_amount=Amount(amount=total_cents, currency="EUR"),
        payment_instrument=INVOICE, iat=now, exp=now + 900,
    )
    return checkout, payment


def standing_limits(manager: JWK, agent: JWK, max_cents: int) -> tuple[str, str]:
    """Manager signs: 'my agent may order from this supplier up to this amount'."""
    now = int(time.time())
    cnf = {"jwk": json.loads(agent.export_public())}
    open_checkout = client.create(
        payloads=[OpenCheckoutMandate(constraints=[AllowedMerchants(allowed=[SUPPLIER])], cnf=cnf, iat=now, exp=now + 86400)],
        issuer_key=manager,
    )
    open_payment = client.create(
        payloads=[OpenPaymentMandate(
            constraints=[AmountRange(currency="EUR", min=0, max=max_cents), AllowedPayees(allowed=[SUPPLIER])],
            cnf=cnf, iat=now, exp=now + 86400,
        )],
        issuer_key=manager,
    )
    return open_checkout, open_payment


def supplier_verifies_chain(manager_pub: JWK, checkout_chain: str, payment_chain: str, checkout_jwt: str, nonce: str) -> list[str]:
    """Supplier side of the autonomous flow. Returns a list of problems (empty = fine)."""
    provider = lambda _token: manager_pub  # noqa: E731  the manager key is the root of trust
    checkout_hash = compute_sha256_b64url(checkout_jwt)
    c_payloads = client.verify(token=checkout_chain, key_or_provider=provider, expected_aud=AUD, expected_nonce=nonce)
    p_payloads = client.verify(token=payment_chain, key_or_provider=provider, expected_aud=AUD, expected_nonce=nonce)
    problems = CheckoutMandateChain.parse(c_payloads).verify(expected_checkout_hash=checkout_hash, checkout_jwt=checkout_jwt)
    problems += PaymentMandateChain.parse(p_payloads).verify(expected_transaction_id=checkout_hash)
    return problems


def main() -> None:
    manager, agent, supplier = new_key("manager-1"), new_key("agent-1"), new_key("supplier-1")
    manager_pub = public(manager)

    print("1. Human not present (auto-approve): 468 EUR order under a 500 EUR standing limit")
    open_checkout, open_payment = standing_limits(manager, agent, max_cents=50000)
    checkout_jwt = signed_checkout(supplier, "chk-1", 46800)
    c_mandate, p_mandate = closed_mandates(checkout_jwt, 46800)
    c_chain = client.present(holder_key=agent, mandate_token=open_checkout, payloads=[c_mandate], nonce="chk-1", aud=AUD)
    p_chain = client.present(holder_key=agent, mandate_token=open_payment, payloads=[p_mandate], nonce="chk-1", aud=AUD)
    problems = supplier_verifies_chain(manager_pub, c_chain, p_chain, checkout_jwt, "chk-1")
    print("   supplier found problems:", problems or "none")
    assert not problems

    reference = compute_sha256_b64url(client.get_closed_mandate_jwt(c_chain))
    receipt = ReceiptClient().create_checkout_receipt(merchant=SUPPLIER.id, reference=reference, order_id="ORD-1")
    receipt_jwt = create_jwt({"alg": "ES256", "typ": "JWT", "kid": "supplier-1"}, receipt.model_dump(mode="json", exclude_none=True), supplier)
    print("   signed receipt verifies, order:", verify_jwt(receipt_jwt, public(supplier))["order_id"])

    print("2. Same standing limit, 780 EUR order: the supplier must refuse")
    big_jwt = signed_checkout(supplier, "chk-2", 78000)
    c_big, p_big = closed_mandates(big_jwt, 78000)
    c_chain = client.present(holder_key=agent, mandate_token=open_checkout, payloads=[c_big], nonce="chk-2", aud=AUD)
    p_chain = client.present(holder_key=agent, mandate_token=open_payment, payloads=[p_big], nonce="chk-2", aud=AUD)
    problems = supplier_verifies_chain(manager_pub, c_chain, p_chain, big_jwt, "chk-2")
    print("   supplier found problems:", problems)
    assert problems

    print("3. Human present (manager clicks Approve): manager signs the exact 780 EUR order")
    direct_checkout = client.create(payloads=[c_big], issuer_key=manager)
    direct_payment = client.create(payloads=[p_big], issuer_key=manager)
    seen_checkout = client.verify(token=direct_checkout, key_or_provider=manager_pub, payload_type=CheckoutMandate).mandate_payload
    seen_payment = client.verify(token=direct_payment, key_or_provider=manager_pub, payload_type=PaymentMandate).mandate_payload
    assert seen_checkout.checkout_hash == compute_sha256_b64url(big_jwt) == seen_payment.transaction_id
    assert seen_payment.payment_amount.amount == 78000
    print("   supplier verified the manager's signature on the exact order")

    print("4. A stranger's key signing the same order: the supplier must refuse")
    forged = client.create(payloads=[c_big], issuer_key=new_key("stranger"))
    try:
        client.verify(token=forged, key_or_provider=manager_pub, payload_type=CheckoutMandate)
        raise AssertionError("forged mandate was accepted")
    except AssertionError:
        raise
    except Exception as error:  # noqa: BLE001  any verification error is the expected outcome
        print("   refused:", type(error).__name__)

    print("\nAP2 SDK works: both flows verified, over-limit and forged orders refused.")


if __name__ == "__main__":
    main()
