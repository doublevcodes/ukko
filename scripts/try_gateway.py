"""Walk through the whole gateway interface as an outside agent would.

Search, signed offers, registration, checkout, AP2 mandates, order, receipts.
Uses throwaway keys and a throwaway buyer name. Needs the gateway running:
    uvicorn gateway.main:app --port 8001

Run:  python scripts/try_gateway.py
"""

from __future__ import annotations

import secrets
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ap2.sdk.generated.types.merchant import Merchant  # noqa: E402
from ap2.sdk.jwt_helper import verify_jwt  # noqa: E402
from jwcrypto.jwk import JWK  # noqa: E402

from app import mandates, settings  # noqa: E402


def main() -> None:
    buyer = f"try-gateway-{secrets.token_hex(3)}"
    gateway = httpx.Client(base_url=settings.GATEWAY_URL, timeout=20, headers={"X-Agent-Id": "try_gateway.py"})
    needed_by = date.today() + timedelta(days=3)

    info = gateway.get("/health").json()
    supplier = Merchant(**info["merchant"])
    supplier_key = JWK(**gateway.get("/.well-known/jwks.json").json()["keys"][0])
    print(f"1. Gateway is up: {info['products']:,} products, supplier '{supplier.name}'.")

    found = gateway.get("/products/search", params={"q": "milk", "claims": ["FREE_FROM_LACTOSE"]}).json()["results"]
    print(f"2. Search 'milk' + FREE_FROM_LACTOSE: {len(found)} products, first: {found[0]['name']}")

    offers = gateway.post("/offers", json={"product_ids": [p["id"] for p in found], "needed_by": str(needed_by)}).json()["offers"]
    orderable = [o for o in offers if o["orderable"]]
    for offer in offers:
        verify_jwt(offer["offer_jwt"], supplier_key)  # raises if the signature is wrong
    print(f"3. {len(offers)} signed offers, all signatures valid, {len(orderable)} orderable by {needed_by}.")
    cheapest = min(orderable, key=lambda o: Decimal(o["comparison_price"]))
    print(f"   Cheapest: {cheapest['name']} at {cheapest['comparison_price']} EUR per {cheapest['pack_unit']}")

    manager, agent = JWK.generate(kty="EC", crv="P-256", kid="manager"), JWK.generate(kty="EC", crv="P-256", kid="agent")
    gateway.post("/buyers", json={"buyer_id": buyer, "public_jwk": mandates.public_jwk(manager)}).raise_for_status()
    standing = mandates.sign_standing_limits(manager, agent, supplier, max_order=Decimal(50), budget=Decimal(200))
    print(f"4. Registered buyer '{buyer}'. Manager signed standing limits: 50 EUR per order, 200 EUR in total.")

    def checkout(request_id: str, quantity: int) -> dict:
        body = {"buyer_id": buyer, "request_id": request_id, "needed_by": str(needed_by),
                "lines": [{"product_id": cheapest["product_id"], "quantity": quantity}]}
        return gateway.post("/checkouts", json=body).json()

    small = checkout("req-small", 10)
    tokens = mandates.sign_as_agent(agent, standing, small, supplier, buyer)
    order = gateway.post(f"/checkouts/{small['checkout_id']}/complete", json={"checkout_mandate": tokens[0], "payment_mandate": tokens[1]}).json()
    print(f"5. Agent alone, 10 units: order {order['order_id']} for {order['total']} EUR. "
          f"Receipts match our mandates: {mandates.receipts_match(order, *tokens, supplier_key)}")

    again = checkout("req-small", 10)
    print(f"6. Same request again: no new order, the gateway returns {again['existing_order']['order_id']}.")

    big = checkout("req-big", 100)
    tokens = mandates.sign_as_agent(agent, standing, big, supplier, buyer)
    refused = gateway.post(f"/checkouts/{big['checkout_id']}/complete", json={"checkout_mandate": tokens[0], "payment_mandate": tokens[1]})
    print(f"7. Agent alone, 100 units: HTTP {refused.status_code}, {refused.json()['detail']['violations']}")

    tokens = mandates.sign_as_manager(manager, big, supplier, buyer)
    order = gateway.post(f"/checkouts/{big['checkout_id']}/complete", json={"checkout_mandate": tokens[0], "payment_mandate": tokens[1]}).json()
    print(f"8. Manager approves the same 100 units: order {order['order_id']} for {order['total']} EUR ({order['flow']}). "
          f"Receipts match: {mandates.receipts_match(order, *tokens, supplier_key)}")

    stranger = JWK.generate(kty="EC", crv="P-256", kid="stranger")
    forged_checkout = checkout("req-forged", 5)
    tokens = mandates.sign_as_manager(stranger, forged_checkout, supplier, buyer)
    refused = gateway.post(f"/checkouts/{forged_checkout['checkout_id']}/complete", json={"checkout_mandate": tokens[0], "payment_mandate": tokens[1]})
    print(f"9. A stranger's key signs an order: HTTP {refused.status_code}, {refused.json()['error']}")
    print("\nSee the supplier's side of all this at", settings.GATEWAY_URL)


if __name__ == "__main__":
    main()
