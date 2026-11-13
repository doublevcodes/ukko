"""The supplier's signing key.

Every offer, checkout and receipt the gateway issues is signed with this key,
so an agent can later prove what the supplier promised. The public half is
published at /.well-known/jwks.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ap2.sdk.jwt_helper import create_jwt
from jwcrypto.jwk import JWK

from gateway import settings

SUPPLIER_KEY_ID = "valio-aimo-gateway-1"
_key: JWK | None = None


def load_or_create(path: Path, key_id: str) -> JWK:
    """Load a P-256 signing key from disk, creating it on first use."""
    if path.exists():
        return JWK(**json.loads(path.read_text(encoding="utf-8")))
    key = JWK.generate(kty="EC", crv="P-256", kid=key_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.export(), encoding="utf-8")
    path.chmod(0o600)  # private key: readable by the owner only
    return key


def supplier_key() -> JWK:
    global _key
    if _key is None:
        _key = load_or_create(settings.GATEWAY_DATA_DIR / "keys" / "supplier.json", SUPPLIER_KEY_ID)
    return _key


def reset() -> None:
    """Forget the cached key (tests use a fresh data folder each time)."""
    global _key
    _key = None


def jwks() -> dict[str, list[dict[str, Any]]]:
    """The public keys, in the standard JWKS shape."""
    return {"keys": [json.loads(supplier_key().export_public())]}


def sign(payload: dict[str, Any]) -> str:
    """Sign a JSON payload as a compact ES256 JWT."""
    header = {"alg": "ES256", "typ": "JWT", "kid": supplier_key()["kid"]}
    return create_jwt(header, payload, supplier_key())
