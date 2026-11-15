"""Buyer trust settings derived from app config (not network)."""

from __future__ import annotations

from app import settings
from app.models import OrderRequest
from app.trust import TrustSettings, is_origin_requirement

APPROVED_ISSUERS = ("valio-aimo-gateway",)


def for_request(request: OrderRequest | None = None) -> TrustSettings:
    needs = ()
    if request is not None:
        needs = tuple(
            need for need in list(request.constraints) + list(request.preferences)
            if is_origin_requirement(need)
        )
    return TrustSettings(
        approved_issuers=APPROVED_ISSUERS,
        audience=settings.BUYER_ID,
        origin_requirements=needs,
    )
