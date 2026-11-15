"""Pipeline step 3: build up to three plans from live offers. Plain code, no language model.

A  the exact item, on time (as much as is in stock)
B  the exact item for what is available, plus substitutes for the rest
C  the exact item, delivered after the deadline

Hard requirements are never broken: an offer that misses one is not used at all.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from app import offer_rules as rules
from app import substitutes
from app.models import Offer, OrderRequest, Plan, PlanLine, SubstituteOption

CENT = Decimal("0.01")
MAX_SUBSTITUTE_LINES = 3


class PlanSet(BaseModel):
    plans: list[Plan] = []  # best first
    substitute_options: list[SubstituteOption] = []
    notes: list[str] = []  # facts for the log, such as why the exact item cannot be used


_SPECIALTY = (
    "coffee milk", "kahvimaito", "latte", "cappuccino", "barista", "chocolate milk",
    "cacao", "milkshake", "protein drink", "cooking cream", "creamer", "milk powder",
)


def _plainness(offer: Offer, request: OrderRequest) -> int:
    """Lower is better. Specialty drinks score high; overlap with the request scores low.

    Price is deliberately ignored: the cheapest quote must not decide what counts as exact.
    """
    name = (offer.name or "").lower()
    score = 0
    for token in _SPECIALTY:
        if token in name:
            score += 100
    wanted = {token for token in (request.item_description + " " + (request.search_terms or "")).lower().split()
              if len(token) > 2 and token not in {"the", "and", "for", "milk", "maito", "free", "lactose"}}
    score -= 5 * sum(1 for token in wanted if token in name)
    score += len(name) // 25
    return score


def choose_exact(request: OrderRequest, offers: list[Offer]) -> str | None:
    """For a typed request, pick the product to treat as "the exact item".

    Prefer one that can supply everything on time, then preference match, then the
    plainest product name. Price does not decide what counts as exact.
    """
    usable = [offer for offer in offers if rules.why_unusable(offer, request) is None]

    def order(offer: Offer) -> tuple:
        wanted = rules.units_for(offer, request, request.quantity)
        complete = rules.on_time(offer, request) and rules.units_in_stock(offer, wanted) == wanted
        return (not complete, -rules.preference_matches(offer, request),
                _plainness(offer, request), -(offer.delivery_reliability or 0), offer.name.lower())

    return min(usable, key=order).product_id if usable else None


def _line(offer: Offer, units: int, request: OrderRequest, option: SubstituteOption | None = None) -> PlanLine:
    return PlanLine(
        offer=offer, quantity=units, line_total=(offer.unit_price * units).quantize(CENT),
        covers=rules.per_sales_unit(offer, request) * units,
        is_substitute=option is not None,
        substitute_reason=option.reason if option else None,
        substitute_tier=option.tier if option else None,
    )


def _footprint(lines: list[PlanLine]) -> Decimal | None:
    """Estimated kilos of CO2 for the plan, or None when any line lacks the data."""
    total = Decimal(0)
    for line in lines:
        if line.offer.co2_kg_per_kg is None or line.offer.kg_per_sales_unit is None:
            return None
        total += line.quantity * line.offer.kg_per_sales_unit * Decimal(str(line.offer.co2_kg_per_kg))
    return total.quantize(Decimal("0.1"))


def _plan(plan_id: str, label: str, lines: list[PlanLine], request: OrderRequest, notes: list[str]) -> Plan:
    covered = sum((line.covers for line in lines), Decimal(0))
    known = [line.offer.delivery_reliability for line in lines if line.offer.delivery_reliability is not None]
    return Plan(
        id=plan_id, label=label, lines=lines, notes=notes,
        total=sum((line.line_total for line in lines), Decimal(0)),
        meets_deadline=all(rules.on_time(line.offer, request) for line in lines),
        meets_constraints=all(not rules.missing_constraints(line.offer, request) for line in lines),
        coverage=min(Decimal(1), covered / request.quantity).quantize(Decimal("0.0001")),
        delivery_date=max(line.offer.earliest_delivery for line in lines),
        reliability=min(known) if known else None,
        co2_kg=_footprint(lines),
    )


def _fill(amount: Decimal, ranked: list[tuple[Offer, SubstituteOption]], request: OrderRequest) -> list[PlanLine]:
    """Cover `amount` with substitutes, best first, moving on when one runs out of stock."""
    lines: list[PlanLine] = []
    for offer, option in ranked:
        if amount <= 0 or len(lines) == MAX_SUBSTITUTE_LINES:
            break
        wanted = rules.units_for(offer, request, amount)
        units = rules.units_in_stock(offer, wanted)
        lines.append(_line(offer, units, request, option))
        amount -= lines[-1].covers
    return lines


def _exact_plan(exact: Offer, request: OrderRequest) -> tuple[Plan, PlanLine]:
    """Plan A (on time) or plan C (late): the exact item only."""
    wanted = rules.units_for(exact, request, request.quantity)
    line = _line(exact, rules.units_in_stock(exact, wanted), request)
    notes = []
    if line.quantity < wanted:
        notes.append(f"Only {line.quantity} of the {wanted} {exact.sales_unit or 'units'} needed are in stock.")
    wishes = [wish for wish in request.preferences if wish not in set(exact.attributes) | set(exact.labels)]
    if wishes:
        notes.append(f"No usable option met the preference {', '.join(wishes)}.")
    if rules.on_time(exact, request):
        label = "Exact item, full quantity, on time" if line.quantity == wanted else "Exact item only, as much as is in stock"
        return _plan("A", label, [line], request, notes), line
    notes.append(f"Earliest delivery is {exact.earliest_delivery:%a %d %b}, after the {request.needed_by:%a %d %b} deadline.")
    return _plan("C", "Exact item, delivered late", [line], request, notes), line


def _substitute_plan(exact_line: PlanLine | None, ranked: list, request: OrderRequest) -> Plan | None:
    """Plan B: what the exact item can cover, plus substitutes for the rest."""
    punctual = [pair for pair in ranked if rules.on_time(pair[0], request)]
    # A late exact item is left out when on-time substitutes exist: the point of B is to arrive on time.
    keep_exact = exact_line is not None and (rules.on_time(exact_line.offer, request) or not punctual)
    base = [exact_line] if keep_exact else []
    remaining = request.quantity - sum((line.covers for line in base), Decimal(0))
    extra = _fill(remaining, punctual or ranked, request) if remaining > 0 else []
    if not extra:
        return None
    label = "Exact item for what is in stock, plus a substitute for the rest" if base else "Substitute for the full quantity"
    return _plan("B", label, base + extra, request, [f"Substitute: {line.offer.name}. {line.substitute_reason}" for line in extra])


def rank_plans(plans: list[Plan]) -> list[Plan]:
    """Meets requirements, then on time, then coverage, then lowest total; reliability breaks ties."""
    return sorted(plans, key=lambda plan: (not plan.meets_constraints, not plan.meets_deadline,
                                           -plan.coverage, plan.total, -(plan.reliability or 0)))


def build_plans(request: OrderRequest, offers: list[Offer], exact_id: str | None) -> PlanSet:
    """Build plans A, B and C where they are possible, ranked best first."""
    by_id = {offer.product_id: offer for offer in offers}
    usable = [offer for offer in offers if rules.why_unusable(offer, request) is None]
    reference = by_id.get(exact_id) if exact_id else None  # may be unusable; still anchors the tiers
    exact = reference if reference in usable else None
    notes, plans, exact_line = [], [], None
    if reference is not None and exact is None:
        notes.append(f"The usual item ({reference.name}) cannot be used: {rules.why_unusable(reference, request)}")
    if exact is not None:
        plan, exact_line = _exact_plan(exact, request)
        plans.append(plan)
    ranked = substitutes.rank(request, reference, usable)
    if exact_line is None or plans[0].coverage < 1 or not plans[0].meets_deadline:
        plan_b = _substitute_plan(exact_line, ranked, request)
        if plan_b:
            plans.append(plan_b)
        elif exact_line is None or plans[0].coverage < 1:
            notes.append("No substitute that meets the requirements is available for the missing amount.")
    used = {line.offer.product_id for plan in plans for line in plan.lines if line.is_substitute}
    return PlanSet(plans=rank_plans(plans), notes=notes, substitute_options=substitutes.shortlist(ranked, used))


def why_not(chosen: Plan, other: Plan, request: OrderRequest) -> str:
    """One sentence on why `other` lost to `chosen`."""
    if chosen.meets_deadline and not other.meets_deadline:
        return f"It would arrive on {other.delivery_date:%a %d %b}, after the {request.needed_by:%a %d %b} deadline."
    if other.coverage < chosen.coverage:
        return f"It covers only {other.coverage:.0%} of the requested quantity."
    if other.total > chosen.total:
        return f"It costs {other.total - chosen.total:.2f} EUR more for the same result."
    return "It ranks lower on delivery reliability."
