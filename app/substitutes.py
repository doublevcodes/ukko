"""Good-better-best ranking of substitutes. Plain code, no language model.

best    Valio lists it as a substitute, or it is the same brand in the same product group
better  the same product group
good    another product group that still meets every hard requirement

Every substitute meets the hard requirements; that is checked before ranking.
"""

from __future__ import annotations

from decimal import Decimal

from app import offer_rules
from app.models import Offer, OrderRequest, SubstituteOption, Tier

TIER_ORDER: dict[Tier, int] = {"best": 0, "better": 1, "good": 2}
MAX_SHOWN = 3


def _brand(offer: Offer) -> str:
    return (offer.brand or "").strip().lower()


def tier_of(offer: Offer, exact: Offer | None, request: OrderRequest) -> tuple[Tier, str]:
    """Which rung a substitute is on, and the reason in plain words."""
    requirements = f" and is marked {', '.join(request.constraints)}" if request.constraints else ""
    if exact is None:
        return "good", f"Matches the request{requirements}."
    if offer.product_id in exact.listed_substitutes:
        return "best", f"Valio lists it as a substitute for {exact.name}{requirements}."
    if offer.category and offer.category == exact.category:
        if _brand(offer) and _brand(offer) == _brand(exact):
            return "best", f"Same brand and product group as {exact.name}{requirements}."
        return "better", f"Same product group as {exact.name}{requirements}."
    return "good", f"A different product group from {exact.name}, but it matches the request{requirements}."


def rank(request: OrderRequest, exact: Offer | None, usable: list[Offer]) -> list[tuple[Offer, SubstituteOption]]:
    """Usable substitutes, best first: tier, then on time, preferences, price, reliability."""
    ranked = []
    for offer in usable:
        if exact is not None and offer.product_id == exact.product_id:
            continue
        tier, reason = tier_of(offer, exact, request)
        option = SubstituteOption(tier=tier, product_id=offer.product_id, name=offer.name,
                                  comparison_price=offer_rules.price_per_unit(offer, request), reason=reason)
        ranked.append((offer, option))

    def order(pair: tuple[Offer, SubstituteOption]) -> tuple:
        offer, option = pair
        return (
            TIER_ORDER[option.tier],
            not offer_rules.on_time(offer, request),
            -offer_rules.preference_matches(offer, request),
            option.comparison_price or Decimal("Infinity"),
            -(offer.delivery_reliability or 0),
        )

    return sorted(ranked, key=order)


def shortlist(ranked: list[tuple[Offer, SubstituteOption]], used_ids: set[str]) -> list[SubstituteOption]:
    """What the dashboard shows: the ones used in a plan, then the next best, three at most."""
    options = [option.model_copy(update={"used_in_plan": option.product_id in used_ids}) for _offer, option in ranked]
    used = [option for option in options if option.used_in_plan]
    rest = [option for option in options if not option.used_in_plan]
    return (used + rest)[:max(MAX_SHOWN, len(used))]
