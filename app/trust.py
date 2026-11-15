"""Buyer-side trust checks for supplier-signed offers.

Pure functions: no network, no global state. A pass proves who made the claim
and that it is unaltered and fresh. It does not prove the claim is true.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ap2.sdk.jwt_helper import verify_jwt
from jwcrypto.jwk import JWK
from jwcrypto.jws import JWS

# Labels / constraints that count as an origin or certificate requirement.
_ORIGIN_NEED_MARKERS = ("ORIGIN", "FROM_FINLAND", "ORGANIC", "HYVAASYOMAASTA", "BLUE_SWAN")


@dataclass(frozen=True)
class TrustCheck:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class TrustSettings:
    """What the buyer requires of a signed offer."""

    approved_issuers: tuple[str, ...]
    audience: str
    max_future_iat_seconds: int = 60
    origin_requirements: tuple[str, ...] = ()  # constraint/preference ids that need origin proof


@dataclass(frozen=True)
class OrderLineQuote:
    """What the buyer intends to order against a signed offer."""

    product_id: str
    unit_price: str
    currency: str
    quantity: int
    delivery_date: str | None
    offer_id: str
    offer_hash: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _b64url_json(segment: str) -> dict[str, Any]:
    padding = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + padding))


def unverified_header(token: str) -> dict[str, Any]:
    return _b64url_json(token.split(".")[0])


def unverified_payload(token: str) -> dict[str, Any]:
    return _b64url_json(token.split(".")[1])


def jwk_for_kid(jwks: dict[str, Any], kid: str | None) -> JWK | None:
    for key in jwks.get("keys") or []:
        if key.get("kid") == kid:
            return JWK(**{k: v for k, v in key.items() if k != "d"})
    return None


def is_origin_requirement(need: str) -> bool:
    upper = need.upper()
    return any(marker in upper for marker in _ORIGIN_NEED_MARKERS)


def _check_signature(token: str, jwks: dict[str, Any]) -> tuple[TrustCheck, dict[str, Any] | None]:
    try:
        header = unverified_header(token)
    except Exception:  # noqa: BLE001
        return TrustCheck("signature", False, "The offer token is not a readable signed JWT."), None
    alg = header.get("alg")
    if alg != "ES256":
        return TrustCheck(
            "signature", False,
            f"Refused: the signature algorithm is '{alg}', not ES256 from an approved supplier key.",
        ), None
    kid = header.get("kid")
    key = jwk_for_kid(jwks, kid)
    if key is None:
        return TrustCheck(
            "signature", False,
            f"Refused: not signed by an approved supplier's key (unknown kid '{kid}').",
        ), None
    try:
        claims = verify_jwt(token, key)
    except Exception:  # noqa: BLE001
        return TrustCheck(
            "signature", False,
            "Refused: the signature does not match its contents, so the price or another field was changed after signing.",
        ), None
    # Defence in depth: jwcrypto accepts the key; reject alg=none style tampering already handled above.
    try:
        jws = JWS()
        jws.deserialize(token)
        if jws.jose_header.get("alg") != "ES256":
            return TrustCheck("signature", False, "Refused: the token header is not ES256."), None
    except Exception:  # noqa: BLE001
        return TrustCheck("signature", False, "Refused: the offer token could not be read."), None
    return TrustCheck(
        "signature", True,
        f"Signature matches the supplier's published key ({kid}).",
    ), claims


def _check_freshness(claims: dict[str, Any], now: int, max_future_iat: int) -> TrustCheck:
    exp = claims.get("exp")
    iat = claims.get("iat")
    if not isinstance(exp, int) or not isinstance(iat, int):
        return TrustCheck("freshness", False, "Refused: the offer is missing issued/expiry times.")
    if exp <= now:
        return TrustCheck("freshness", False, "Refused: the offer has expired.")
    if iat > now + max_future_iat:
        return TrustCheck("freshness", False, "Refused: the offer claims to be issued in the future.")
    minutes = max(0, (exp - now) // 60)
    return TrustCheck("freshness", True, f"Offer is fresh ({minutes} minute(s) left).")


def _check_supplier(claims: dict[str, Any], approved: tuple[str, ...]) -> TrustCheck:
    iss = claims.get("iss")
    if iss not in approved:
        return TrustCheck(
            "supplier", False,
            f"Refused: issuer '{iss}' is not on the approved supplier list.",
        )
    return TrustCheck("supplier", True, f"Issuer '{iss}' is an approved supplier.")


def _check_audience(claims: dict[str, Any], audience: str) -> TrustCheck:
    aud = claims.get("aud")
    if aud != audience:
        return TrustCheck(
            "audience", False,
            "Refused: the offer was made for someone else.",
        )
    return TrustCheck("audience", True, f"Offer was made for this buyer ({audience}).")


def _check_order_match(claims: dict[str, Any], token: str, line: OrderLineQuote) -> TrustCheck:
    product = claims.get("product") or {}
    price = claims.get("price") or {}
    stock = claims.get("stock") or {}
    if claims.get("jti") != line.offer_id:
        return TrustCheck("order_matches_offer", False, "The order does not quote this offer's id.")
    if token_hash(token) != line.offer_hash:
        return TrustCheck("order_matches_offer", False, "The order's offer hash does not match the signed token.")
    if product.get("product_id") != line.product_id:
        return TrustCheck("order_matches_offer", False, "The order product does not match the signed offer.")
    if str(price.get("unit_price")) != str(line.unit_price) or price.get("currency") != line.currency:
        return TrustCheck("order_matches_offer", False, "The order price does not match the signed offer.")
    available = stock.get("available_quantity")
    if available is not None and line.quantity > int(available):
        return TrustCheck(
            "order_matches_offer", False,
            f"The order quantity ({line.quantity}) is above the signed available stock ({available}).",
        )
    earliest = stock.get("earliest_delivery")
    if line.delivery_date and earliest and line.delivery_date < earliest:
        return TrustCheck(
            "order_matches_offer", False,
            f"The delivery date {line.delivery_date} is before the signed earliest delivery {earliest}.",
        )
    return TrustCheck("order_matches_offer", True, "Order matches the signed offer.")


def _check_not_reused(claims: dict[str, Any], used_offer_ids: set[str]) -> TrustCheck:
    jti = claims.get("jti")
    if jti in used_offer_ids:
        return TrustCheck("not_reused", False, "Refused: this offer was already used for a placed order.")
    return TrustCheck("not_reused", True, "This offer id has not been used for an order yet.")


def _check_origin(claims: dict[str, Any], requirements: tuple[str, ...]) -> TrustCheck:
    needed = [need for need in requirements if is_origin_requirement(need)]
    if not needed:
        return TrustCheck("origin", True, "No origin or certificate requirement on this request.")
    origin = claims.get("origin") or {}
    country = origin.get("country")
    cert_ids = {c.get("id") for c in (origin.get("certificates") or []) if isinstance(c, dict)}
    missing = []
    for need in needed:
        if need in cert_ids:
            continue
        # A Finnish-origin style need can also be met by country_of_origin "fi".
        if "FINLAND" in need.upper() and country == "fi":
            continue
        missing.append(need)
    if missing:
        stated = "Origin not stated by supplier" if country is None and not cert_ids else (
            f"country={country!r}, certificates={sorted(cert_ids)}"
        )
        return TrustCheck(
            "origin", False,
            f"The buyer asked for {', '.join(missing)}. The supplier's signed offer does not state it "
            f"({stated}), so a person must approve.",
        )
    return TrustCheck("origin", True, "Signed origin/certificate fields meet the buyer's requirements.")


def verify_offer(
    token: str,
    jwks: dict[str, Any],
    now: int,
    settings: TrustSettings,
    *,
    order_line: OrderLineQuote | None = None,
    used_offer_ids: set[str] | None = None,
) -> list[TrustCheck]:
    """Run the trust-protocol checks. Checks 1–4 always; 5–7 when ordering context is given."""
    signature, claims = _check_signature(token, jwks)
    checks = [signature]
    if claims is None:
        # Still report the later checks as failed so the UI has a full list.
        checks.append(TrustCheck("freshness", False, "Skipped: the signature did not verify."))
        checks.append(TrustCheck("supplier", False, "Skipped: the signature did not verify."))
        checks.append(TrustCheck("audience", False, "Skipped: the signature did not verify."))
        if order_line is not None:
            checks.append(TrustCheck("order_matches_offer", False, "Skipped: the signature did not verify."))
            checks.append(TrustCheck("not_reused", False, "Skipped: the signature did not verify."))
            checks.append(TrustCheck("origin", False, "Skipped: the signature did not verify."))
        return checks

    checks.append(_check_freshness(claims, now, settings.max_future_iat_seconds))
    checks.append(_check_supplier(claims, settings.approved_issuers))
    checks.append(_check_audience(claims, settings.audience))
    if order_line is not None:
        checks.append(_check_order_match(claims, token, order_line))
        checks.append(_check_not_reused(claims, used_offer_ids or set()))
        checks.append(_check_origin(claims, settings.origin_requirements))
    return checks


def lookup_ok(checks: list[TrustCheck]) -> bool:
    """Checks 1–4 must pass for an offer to enter planning."""
    needed = {"signature", "freshness", "supplier", "audience"}
    by_name = {check.name: check for check in checks}
    return all(by_name.get(name) and by_name[name].passed for name in needed)


def blocks_offer(checks: list[TrustCheck]) -> bool:
    """Failed signature, supplier or audience cannot be approved around."""
    return any(not c.passed and c.name in {"signature", "supplier", "audience"} for c in checks)


def needs_origin_approval(checks: list[TrustCheck]) -> bool:
    return any(not c.passed and c.name == "origin" for c in checks)


def money_str(value: Decimal | str | None) -> str:
    if value is None:
        return ""
    return str(Decimal(str(value)))


def delivery_iso(value: date | str | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
