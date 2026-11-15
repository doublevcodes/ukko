"""Trust-protocol verification: pure checks against ES256/AP2 signed offers."""

from __future__ import annotations

import base64
import hashlib
import json
import time

from jwcrypto.jwk import JWK

from app.trust import OrderLineQuote, TrustSettings, lookup_ok, needs_origin_approval, verify_offer
from gateway import keys, settings
from gateway.offers import signed_claims


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unsigned_token(header: dict, payload: dict, signature: str = "") -> str:
    return f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(payload, separators=(',', ':')).encode())}.{signature}"


def _settings(**overrides) -> TrustSettings:
    base = dict(
        approved_issuers=(settings.MERCHANT_ID,),
        audience="stockpot-demo-kitchen",
        origin_requirements=(),
    )
    base.update(overrides)
    return TrustSettings(**base)


def _fresh_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": settings.MERCHANT_ID,
        "aud": "stockpot-demo-kitchen",
        "jti": "off_test_valid",
        "iat": now,
        "exp": now + 600,
        "product": {"product_id": "1001", "gtin": "6400000001001", "name": "Test milk"},
        "price": {"unit_price": "1.50", "currency": "EUR", "vat_basis": "excl", "per": "piece", "pack_size": 1},
        "stock": {"available_quantity": 100, "earliest_delivery": "2026-11-16"},
        "origin": {"country": "fi", "producer": "Valio Oy", "certificates": []},
    }
    claims.update(overrides)
    return claims


def _sign(claims: dict, key: JWK | None = None) -> str:
    key = key or keys.supplier_key()
    from ap2.sdk.jwt_helper import create_jwt
    header = {"alg": "ES256", "typ": "JWT", "kid": key["kid"]}
    return create_jwt(header, claims, key)


def test_valid_offer_passes_checks_1_to_4():
    token = _sign(_fresh_claims())
    checks = verify_offer(token, keys.jwks(), int(time.time()), _settings())
    assert lookup_ok(checks)
    assert all(c.passed for c in checks)


def test_price_changed_after_signing_refused():
    token = _sign(_fresh_claims())
    header, payload, signature = token.split(".")
    # Tamper with the payload without re-signing.
    data = json.loads(base64.urlsafe_b64decode(payload + "=="))
    data["price"]["unit_price"] = "9.99"
    forged = f"{header}.{_b64(json.dumps(data, separators=(',', ':')).encode())}.{signature}"
    checks = verify_offer(forged, keys.jwks(), int(time.time()), _settings())
    assert not lookup_ok(checks)
    assert any(c.name == "signature" and not c.passed for c in checks)


def test_expired_refused():
    now = int(time.time())
    token = _sign(_fresh_claims(iat=now - 1200, exp=now - 600))
    checks = verify_offer(token, keys.jwks(), now, _settings())
    assert any(c.name == "freshness" and not c.passed for c in checks)


def test_issued_in_future_refused():
    now = int(time.time())
    token = _sign(_fresh_claims(iat=now + 3600, exp=now + 4200))
    checks = verify_offer(token, keys.jwks(), now, _settings())
    assert any(c.name == "freshness" and not c.passed for c in checks)


def test_unknown_kid_refused():
    token = _sign(_fresh_claims())
    jwks = {"keys": [{**keys.public_jwk(), "kid": "other-kid"}]}
    checks = verify_offer(token, jwks, int(time.time()), _settings())
    assert any(c.name == "signature" and not c.passed for c in checks)


def test_right_kid_wrong_key_refused():
    other = JWK.generate(kty="EC", crv="P-256", kid=keys.SUPPLIER_KEY_ID)
    token = _sign(_fresh_claims(), key=other)
    checks = verify_offer(token, keys.jwks(), int(time.time()), _settings())
    assert any(c.name == "signature" and not c.passed for c in checks)


def test_wrong_audience_refused():
    token = _sign(_fresh_claims(aud="other-kitchen"))
    checks = verify_offer(token, keys.jwks(), int(time.time()), _settings())
    assert any(c.name == "audience" and not c.passed for c in checks)


def test_issuer_not_approved_refused():
    token = _sign(_fresh_claims(iss="imposter-gateway"))
    checks = verify_offer(token, keys.jwks(), int(time.time()), _settings())
    assert any(c.name == "supplier" and not c.passed for c in checks)


def test_alg_none_refused():
    token = _unsigned_token({"alg": "none", "typ": "JWT"}, _fresh_claims())
    checks = verify_offer(token, keys.jwks(), int(time.time()), _settings())
    assert any(c.name == "signature" and not c.passed for c in checks)


def test_hs256_refused():
    token = _unsigned_token(
        {"alg": "HS256", "typ": "JWT", "kid": keys.SUPPLIER_KEY_ID},
        _fresh_claims(),
        signature=_b64(b"not-a-real-hmac"),
    )
    checks = verify_offer(token, keys.jwks(), int(time.time()), _settings())
    assert any(c.name == "signature" and not c.passed for c in checks)


def test_order_price_mismatch_refused():
    claims = _fresh_claims()
    token = _sign(claims)
    line = OrderLineQuote(
        product_id="1001", unit_price="9.99", currency="EUR", quantity=1,
        delivery_date="2026-11-16", offer_id=claims["jti"],
        offer_hash=hashlib.sha256(token.encode()).hexdigest(),
    )
    checks = verify_offer(token, keys.jwks(), int(time.time()), _settings(), order_line=line)
    assert any(c.name == "order_matches_offer" and not c.passed for c in checks)


def test_reused_offer_refused():
    claims = _fresh_claims()
    token = _sign(claims)
    line = OrderLineQuote(
        product_id="1001", unit_price="1.50", currency="EUR", quantity=1,
        delivery_date="2026-11-16", offer_id=claims["jti"],
        offer_hash=hashlib.sha256(token.encode()).hexdigest(),
    )
    checks = verify_offer(
        token, keys.jwks(), int(time.time()), _settings(),
        order_line=line, used_offer_ids={claims["jti"]},
    )
    assert any(c.name == "not_reused" and not c.passed for c in checks)


def test_origin_required_but_null_needs_approval():
    claims = _fresh_claims(origin={"country": None, "producer": None, "certificates": []})
    token = _sign(claims)
    line = OrderLineQuote(
        product_id="1001", unit_price="1.50", currency="EUR", quantity=1,
        delivery_date="2026-11-16", offer_id=claims["jti"],
        offer_hash=hashlib.sha256(token.encode()).hexdigest(),
    )
    checks = verify_offer(
        token, keys.jwks(), int(time.time()),
        _settings(origin_requirements=("GOODS_FROM_FINLAND_BLUE_SWAN",)),
        order_line=line,
    )
    assert needs_origin_approval(checks)


def test_signed_claims_helper_matches_minimum_fields():
    from datetime import datetime, timedelta
    issued = datetime.now().astimezone().replace(microsecond=0)
    expires = issued + timedelta(seconds=600)
    offer = {
        "product_id": "1001", "name": "Test milk", "gtin": "123", "unit_price": "1.50",
        "currency": "EUR", "pieces_per_sales_unit": "1", "available_quantity": 10,
        "earliest_delivery": "2026-11-16", "country_of_origin": "fi", "producer": "Valio Oy",
        "labels": ["EU_ORGANIC_FARMING"],
    }
    claims = signed_claims(offer, "stockpot-demo-kitchen", issued, expires, "off_abc")
    assert set(claims) >= {"iss", "aud", "jti", "iat", "exp", "product", "price", "stock", "origin"}
    assert claims["price"]["unit_price"] == "1.50"
    assert claims["origin"]["certificates"][0]["id"] == "EU_ORGANIC_FARMING"
