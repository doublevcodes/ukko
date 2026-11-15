"""Deliberately broken signed offers for the buyer's Trust test tab.

The private key stays on the gateway. The buyer only verifies. These fixtures
do not call Valio: they mint a minimal signed offer so the tab works offline.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any

from ap2.sdk.jwt_helper import create_jwt
from jwcrypto.jwk import JWK

from gateway import keys, offer_store, settings


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _fresh_claims(buyer_id: str) -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": settings.MERCHANT_ID,
        "aud": buyer_id,
        "jti": f"off_{secrets.token_hex(8)}",
        "iat": now,
        "exp": now + settings.OFFER_TTL_SECONDS,
        "product": {
            "product_id": "trust-test",
            "gtin": None,
            "name": "Trust-test butter 500 g",
        },
        "price": {
            "unit_price": "3.82",
            "currency": settings.CURRENCY,
            "vat_basis": "excl",
            "per": "piece",
            "pack_size": 1,
        },
        "stock": {"available_quantity": 100, "earliest_delivery": "2026-11-16"},
        "origin": {"country": "fi", "producer": "Valio Oy", "certificates": []},
    }


def _sign(claims: dict[str, Any]) -> str:
    token = keys.sign(claims)
    offer_store.remember(token, claims)
    return token


def broken_offer(scenario: str, buyer_id: str) -> dict[str, Any]:
    claims = _fresh_claims(buyer_id)
    token = _sign(claims)
    product_id = claims["product"]["product_id"]

    if scenario == "price_changed":
        header, payload, signature = token.split(".")
        data = json.loads(base64.urlsafe_b64decode(payload + "=="))
        data["price"]["unit_price"] = "99.99"
        forged = f"{header}.{_b64(json.dumps(data, separators=(',', ':')).encode())}.{signature}"
        return {"offer_jwt": forged, "offer_claims": data, "product_id": product_id, "offer_id": claims["jti"]}

    if scenario == "expired":
        claims["iat"] = int(time.time()) - 1200
        claims["exp"] = int(time.time()) - 600
        claims["jti"] = f"off_{secrets.token_hex(8)}"
        token = _sign(claims)
        return {"offer_jwt": token, "offer_claims": claims, "product_id": product_id, "offer_id": claims["jti"]}

    if scenario == "unknown_signer":
        other = JWK.generate(kty="EC", crv="P-256", kid="imposter-key")
        token = create_jwt({"alg": "ES256", "typ": "JWT", "kid": "imposter-key"}, claims, other)
        return {"offer_jwt": token, "offer_claims": claims, "product_id": product_id, "offer_id": claims["jti"]}

    if scenario == "reused":
        offer_store.mark_used(claims["jti"], "VA-TEST-USED")
        return {
            "offer_jwt": token, "offer_claims": claims, "product_id": product_id,
            "offer_id": claims["jti"], "used_offer_ids": [claims["jti"]],
        }

    if scenario == "wrong_buyer":
        claims["aud"] = "someone-elses-kitchen"
        claims["jti"] = f"off_{secrets.token_hex(8)}"
        token = _sign(claims)
        return {"offer_jwt": token, "offer_claims": claims, "product_id": product_id, "offer_id": claims["jti"]}

    raise ValueError(f"Unknown scenario '{scenario}'.")
