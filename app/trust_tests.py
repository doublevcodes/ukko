"""Settings → Trust test: break a real signed offer and run the pipeline verifier."""

from __future__ import annotations

import time
from typing import Any

from app import gateway_client, settings, state, trust
from app.trust import OrderLineQuote

SCENARIOS = {
    "price_changed": "Refused: the signature does not match the contents",
    "expired": "Refused: the offer has expired",
    "unknown_signer": "Refused: not signed by an approved supplier's key",
    "reused": "Refused: this offer was already used",
    "wrong_buyer": "Refused: the offer was made for someone else",
}


def run(scenario: str) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario '{scenario}'. Choose one of: {', '.join(SCENARIOS)}.")

    # The gateway mints a deliberately broken offer so the buyer never uses a private key.
    raw = gateway_client._call(  # noqa: SLF001  same public HTTP door as the rest of Ukko
        "POST", "/trust-tests/broken-offer",
        json={"scenario": scenario, "buyer_id": settings.BUYER_ID},
    )
    token = raw["offer_jwt"]
    claims = trust.unverified_payload(token) if token.count(".") == 2 else {}
    # For price_changed the payload is forged; still readable.
    try:
        claims = trust.unverified_payload(token)
    except Exception:  # noqa: BLE001
        claims = raw.get("offer_claims") or {}

    line = OrderLineQuote(
        product_id=str((claims.get("product") or {}).get("product_id") or raw.get("product_id") or "1001"),
        unit_price=str((claims.get("price") or {}).get("unit_price") or raw.get("unit_price") or "0"),
        currency=str((claims.get("price") or {}).get("currency") or "EUR"),
        quantity=1,
        delivery_date=(claims.get("stock") or {}).get("earliest_delivery"),
        offer_id=str(claims.get("jti") or raw.get("offer_id") or "off_test"),
        offer_hash=trust.token_hash(token),
    )
    used = set(raw.get("used_offer_ids") or [])

    checks = gateway_client.verify_raw_offer(
        token, order_line=line, used_offer_ids=used, now=int(time.time()),
    )
    result = {
        "scenario": scenario,
        "expected": SCENARIOS[scenario],
        "checks": [{"name": c.name, "passed": c.passed, "reason": c.reason} for c in checks],
        "refused": any(not c.passed for c in checks),
        "test": True,
    }
    failed = next((c for c in checks if not c.passed), None)
    summary = (
        f"Trust test ({scenario}): {failed.reason if failed else 'unexpected pass'}. "
        f"Expected: {SCENARIOS[scenario]}. Marked as a test — no order was placed."
    )
    state.log(None, "trust_test", summary, result=result)
    return result
