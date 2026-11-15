"""T1: the supplier publishes a public key; the private half stays on disk."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from gateway import keys, settings
from gateway.main import app as gateway_app


def test_key_created_on_first_use_and_persisted():
    path = keys.key_path()
    assert not path.exists()
    first = keys.supplier_key()
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["kty"] == "EC"
    assert on_disk["crv"] == "P-256"
    assert on_disk["kid"] == keys.SUPPLIER_KEY_ID
    assert "d" in on_disk  # private material stays in the file only

    keys.reset()
    second = keys.supplier_key()
    assert second["x"] == first["x"]
    assert second["y"] == first["y"]


def test_jwks_publishes_public_key_only():
    published = keys.jwks()
    assert list(published.keys()) == ["keys"]
    assert len(published["keys"]) == 1
    jwk = published["keys"][0]
    assert jwk["kty"] == "EC"
    assert jwk["crv"] == "P-256"
    assert jwk["alg"] == "ES256"
    assert jwk["use"] == "sig"
    assert jwk["kid"] == keys.SUPPLIER_KEY_ID
    assert "d" not in jwk
    assert "x" in jwk and "y" in jwk


def test_well_known_jwks_endpoint():
    client = TestClient(gateway_app)
    response = client.get("/.well-known/jwks.json")
    assert response.status_code == 200
    body = response.json()
    assert body == keys.jwks()
    assert "d" not in body["keys"][0]
    # Private key file must not live under the project root outside data/.
    assert str(settings.GATEWAY_DATA_DIR).endswith("/data/gateway") or "ukko-tests-" in str(
        settings.GATEWAY_DATA_DIR
    )
