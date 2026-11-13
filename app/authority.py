"""The agent's signed authority to order (AP2), kept ready for the pipeline.

Holds the two keys and the current standing limits. The limits are signed
again whenever the policy is saved, so the supplier always enforces the same
spend limit the dashboard shows.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from ap2.sdk.generated.types.merchant import Merchant
from jwcrypto.jwk import JWK

from app import gateway_client, mandates, settings
from app.models import PolicyConfig

RENEW_BEFORE_EXPIRY_SECONDS = 3600
_lock = threading.Lock()
_standing: mandates.StandingLimits | None = None
_supplier: Merchant | None = None


def manager_key() -> JWK:
    """Stands for the kitchen manager's signing device. Used only when a person approves."""
    return mandates.load_or_create_key(settings.KEYS_DIR / "manager.json", "stockpot-manager-1")


def agent_key() -> JWK:
    return mandates.load_or_create_key(settings.KEYS_DIR / "agent.json", "stockpot-agent-1")


def supplier() -> Merchant:
    """Who we order from, as the gateway names itself."""
    global _supplier
    if _supplier is None:
        _supplier = Merchant(**gateway_client.health()["merchant"])
    return _supplier


def renew(policy: PolicyConfig) -> mandates.StandingLimits:
    """Register the manager's key with the supplier and sign fresh standing limits."""
    global _standing
    with _lock:
        gateway_client.register_buyer(mandates.public_jwk(manager_key()))
        _standing = mandates.sign_standing_limits(
            manager_key(), agent_key(), supplier(),
            max_order=policy.auto_approve_max_total_eur, budget=policy.agent_budget_eur,
        )
        return _standing


def ensure(policy: PolicyConfig) -> mandates.StandingLimits:
    """Standing limits that match the current policy, signing new ones only when needed."""
    current = _standing
    fresh = (
        current is not None
        and current.max_order_eur == policy.auto_approve_max_total_eur
        and current.budget_eur == policy.agent_budget_eur
        and current.expires_at - time.time() > RENEW_BEFORE_EXPIRY_SECONDS
    )
    if fresh:
        # The key is registered again each time: it is cheap, and it survives a gateway restart.
        gateway_client.register_buyer(mandates.public_jwk(manager_key()))
        return current
    return renew(policy)


def forget() -> None:
    """Drop the cached limits and supplier (used when the policy changes while the gateway is down)."""
    global _standing, _supplier
    _standing, _supplier = None, None


def summary() -> dict[str, Any] | None:
    """What the dashboard shows about the agent's signed authority."""
    if _standing is None:
        return None
    return {
        "supplier": _standing.supplier_id,
        "max_order_eur": str(_standing.max_order_eur),
        "budget_eur": str(_standing.budget_eur),
        "signed_at": _standing.issued_at,
        "expires_at": _standing.expires_at,
        "reference": mandates.mandate_digest(_standing.open_payment)[:16],
    }
