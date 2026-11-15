"""The fixed pipeline: eight steps, one function each, always in the same order.

1 parse -> 2 lookup -> 3 build plans -> 4 web checks -> 5 choose and explain
-> 6 policy check -> 7 order or ask a person -> 8 finalise

Each step writes to the audit log and handles its own errors. The language
model helps in steps 1, 2, 4 and 5; it never decides whether to order.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from app import explain, forecast, gateway_client, llm, lookup as lookup_step, ordering, parsing, planner, policy, state
from app.config import ShelfProduct
from app.gateway_client import GatewayError, GatewayRefused
from app.models import Alternative, OrderRequest, Plan, PolicyResult, WebFinding
from app.planner import PlanSet
from app.state import RequestRecord
from app.web import plan_checks

MAX_REPLANS = 2
NOTHING_ORDERED = "Nothing was ordered."
_vocabulary_cache: dict[str, Any] | None = None


def _vocabulary() -> dict[str, Any]:
    """The supplier's list of checkable requirements, fetched once."""
    global _vocabulary_cache
    if _vocabulary_cache is None:
        _vocabulary_cache = gateway_client.vocabulary()
    return _vocabulary_cache


def fail(record: RequestRecord, step: str, message: str) -> None:
    record.decision.status, record.decision.message = "failed", f"{message} {NOTHING_ORDERED}"
    state.log(record.id, step, record.decision.message)


def _ask(record: RequestRecord, step: str, question: str) -> None:
    record.decision.status, record.decision.message = "needs_clarification", question
    state.log(record.id, step, f"Asked a question instead of guessing: {question}")


def parse_request(record: RequestRecord, text: str) -> bool:
    """Step 1 (typed): the language model reads the text; code refuses to guess."""
    try:
        request, question, notes = parsing.parse(text, record.id, _vocabulary(), state.now().date())
    except GatewayError as error:
        fail(record, "parse", f"The supplier could not be reached. {error}")
        return False
    except llm.LLMError as error:
        fail(record, "parse", f"The request could not be read. {error}")
        return False
    if question:
        _ask(record, "parse", question)
        return False
    record.request = request
    must = f", must be {', '.join(request.constraints)}" if request.constraints else ""
    wish = f", ideally {', '.join(request.preferences)}" if request.preferences else ""
    state.log(record.id, "parse", f"Understood: {request.quantity} {request.unit} of {request.item_description} "
              f"by {request.needed_by:%a %d %b}{must}{wish}. {' '.join(notes)}".strip(), request=request.model_dump(mode="json"))
    return True


def _forecast(key: str, settings: forecast.Stocked) -> forecast.Forecast:
    """How much to order for `key`, from its usage, bookings and what is probably on the shelf now.

    The live shelf row's settings win, so the amount matches the forecast check that raised the order.
    """
    row = state.shelf.get(key)
    settings = row or settings
    level = row.level if row else None
    stock = forecast.stock_now(settings, level, row.updated_at if row else None, forecast.rate_for(key, settings),
                               state.now(), row.quantity if row else None)
    return forecast.reorder(key, settings, level, state.now().date(), on_shelf=stock)


def request_from_shelf(record: RequestRecord, product: ShelfProduct) -> bool:
    """Step 1 (sensor): no language model; the settings come from shelf.yaml, the amount from app/forecast.py."""
    today = state.now().date()
    needed_by = today + timedelta(days=product.lead_time_days)
    amount = _forecast(record.product_key, product)
    record.request = OrderRequest(
        id=record.id, source="sensor", item_description=product.name, quantity=amount.quantity,
        unit=product.unit, needed_by=needed_by, constraints=product.constraints, preferences=product.preferences,
        search_terms=product.search_terms, product_id=product.product_id, usual_quantity=product.usual_quantity,
    )
    state.log(record.id, "parse", f"Reorder: {amount.quantity} {product.unit} of {product.name}, "
              f"needed by {needed_by:%a %d %b}. {amount.explanation}", forecast=amount.model_dump(mode="json"), request=record.request.model_dump(mode="json"))
    return True


def request_from_vision(record: RequestRecord, row: state.ShelfLevel) -> bool:
    """Step 1 for a camera-recognised product: exact Valio id when known, otherwise catalogue search."""
    unit = row.unit
    lead = row.lead_time_days if row.lead_time_days is not None else 2
    if row.reorder_quantity is None or unit is None:
        fail(record, "parse", "This camera-seen product has no reorder quantity or unit configured.")
        return False
    needed_by = state.now().date() + timedelta(days=lead)
    amount = _forecast(record.product_key, row)
    quantity = amount.quantity
    terms = (row.search_terms or row.name).strip()
    record.request = OrderRequest(
        id=record.id, source="sensor", item_description=row.name, quantity=quantity, unit=unit,
        needed_by=needed_by, constraints=list(row.constraints), preferences=list(row.preferences),
        search_terms=terms, product_id=row.product_id, usual_quantity=row.usual_quantity,
    )
    if row.product_id:
        state.log(record.id, "parse",
                  f"Camera-seen product '{row.name}' maps to Valio {row.product_id}: "
                  f"{quantity} {unit}, needed by {needed_by:%a %d %b}. {amount.explanation}",
                  forecast=amount.model_dump(mode="json"), request=record.request.model_dump(mode="json"))
    else:
        state.log(record.id, "parse",
                  f"Camera-seen product '{row.name}' is not an exact catalogue SKU; "
                  f"will match the closest Valio item for {quantity} {unit}, needed by {needed_by:%a %d %b}. "
                  f"{amount.explanation}", forecast=amount.model_dump(mode="json"), request=record.request.model_dump(mode="json"))
    return True


def lookup(record: RequestRecord) -> bool:
    """Step 2: search the catalogue and get live signed offers for the candidates."""
    request = record.request
    try:
        found = lookup_step.find(request, _vocabulary())
    except (GatewayError, GatewayRefused) as error:
        fail(record, "lookup", f"The supplier could not answer. {error}")
        return False
    if found.ask:
        _ask(record, "lookup", found.ask)
        return False
    if not found.offers:
        needs = f" marked {', '.join(request.constraints)}" if request.constraints else ""
        nearby = next((note[len("Nearby catalogue names: "):] for note in found.notes
                       if note.startswith("Nearby catalogue names: ")), "")
        hint = (f" Closest names in the catalogue: {nearby}. Reply with one of those, or describe it differently."
                if nearby else " Can you describe it differently, or name a similar product Valio sells?")
        _ask(record, "lookup", f"I found no product{needs} matching '{request.item_description or request.search_terms}' "
             f"in Valio's catalogue.{hint}")
        return False
    record.offers, record.candidate_ids, record.exact_id = found.offers, found.candidate_ids, found.exact_id
    orderable = sum(1 for offer in found.offers if offer.orderable)
    kid = "valio-aimo-2026-11"
    if found.offers and found.offers[0].trust_checks:
        for check in found.offers[0].trust_checks:
            if check.get("name") == "signature" and "(" in (check.get("reason") or ""):
                kid = (check["reason"].rsplit("(", 1)[-1] or "").rstrip(").") or kid
                break
    valid_until = ""
    expiries = [o.expires_at for o in found.offers if o.expires_at]
    if expiries:
        valid_until = f" Valid until {min(expiries):%H:%M}."
    state.log(
        record.id, "lookup",
        f"Got {len(found.offers)} signed offers from Valio. All {len(found.offers)} signatures match "
        f"Valio's published key ({kid}).{valid_until} "
        f"{orderable} can be ordered now. {' '.join(found.notes)}".strip(),
        offers=[{"product_id": o.product_id, "name": o.name, "unit_price": str(o.unit_price), "orderable": o.orderable,
                 "available": o.available_quantity, "offer_id": o.offer_id,
                 "pieces_per_sales_unit": str(o.pieces_per_sales_unit)} for o in found.offers],
    )
    for refusal in found.refused:
        state.log(
            record.id, "lookup",
            f"Refused an offer for {refusal.get('name') or refusal.get('product_id')}: "
            f"{refusal.get('reason')} It was not used.",
            refusal=refusal,
        )
    return True


def build_plans(record: RequestRecord) -> PlanSet | None:
    """Step 3: plain code builds plans A, B and C where they are possible."""
    plans = planner.build_plans(record.request, record.offers, record.exact_id)
    record.decision.substitute_options = plans.substitute_options
    if not plans.plans:
        detail = " ".join(plans.notes).strip()
        why = detail or "No candidate can be ordered and still meet the requirements."
        fail(record, "plans", f"No plan is possible. {why}")
        return None
    listing = "; ".join(f"Plan {p.id} ({p.label}): {p.total:.2f} EUR, covers {p.coverage:.0%}" for p in plans.plans)
    state.log(record.id, "plans", f"Built {len(plans.plans)} plan(s). {listing}. {' '.join(plans.notes)}".strip())
    return plans


def _in_words(findings: list[WebFinding]) -> str:
    """One readable line for the log. Checks that failed together are reported once."""
    ran = [f"{policy.CHECK_NAMES[f.check_type].capitalize()}: {f.summary}" for f in findings if f.severity != "check_failed"]
    failed = [finding for finding in findings if finding.severity == "check_failed"]
    if failed:
        ran.append(f"{len(failed)} check(s) could not be completed. {failed[0].summary}")
    return " ".join(ran)


def web_checks(record: RequestRecord, plans: list[Plan]) -> tuple[list[Plan], list[WebFinding], list[Alternative]]:
    """Step 4: check the best plan on the live web. A blocker removes the plan and the next is checked."""
    removed: list[Alternative] = []
    while True:
        top = plans[0]
        findings = plan_checks.for_plan(record.request, top)
        state.log(record.id, "web_checks", f"Web checks for Plan {top.id}: {_in_words(findings)}",
                  findings=[finding.model_dump(mode="json") for finding in findings])
        blockers = [finding for finding in findings if finding.severity == "blocker"]
        if not blockers or len(plans) == 1:
            return plans, findings, removed
        removed.append(Alternative(plan=top, why_not=f"Removed after a web check: {blockers[0].summary}"))
        state.log(record.id, "web_checks", f"Plan {top.id} was removed because a web check found a serious problem.")
        plans = plans[1:]


def choose_and_explain(record: RequestRecord, plans: list[Plan], findings: list[WebFinding], removed: list[Alternative]) -> None:
    """Step 5: code has ranked the plans; the language model only words the explanation."""
    chosen, request, decision = plans[0], record.request, record.decision
    others = [Alternative(plan=plan, why_not=planner.why_not(chosen, plan, request)) for plan in plans[1:]]
    decision.chosen_plan, decision.alternatives, decision.web_findings = chosen, others + removed, findings
    decision.explanation, problem = explain.write(request, chosen, decision.alternatives, findings)
    note = f" (Explanation written from a template: {problem})" if problem else ""
    # Keep the supplier's signed offers for the chosen lines: proof of what the agent was told.
    state.log(record.id, "choose", f"Chose Plan {chosen.id}: {chosen.label}, {chosen.total:.2f} EUR.{note}",
              signed_offers={line.offer.product_id: line.offer.offer_jwt for line in chosen.lines})


def policy_check(record: RequestRecord) -> None:
    """Step 6: ordinary code decides between ordering, asking a person, and refusing."""
    decision = record.decision
    decision.policy_result = policy.evaluate(record.request, decision.chosen_plan, decision.web_findings, state.policy)
    words = {"auto_approve": "the agent may order on its own", "needs_approval": "a person must approve", "deny": "refused"}
    state.log(record.id, "policy", f"Rules checked: {words[decision.policy_result.outcome]}. "
              + " ".join(decision.policy_result.reasons), policy=state.policy.model_dump(mode="json"))


def _wait_for_person(record: RequestRecord) -> None:
    record.approval_token = secrets.token_urlsafe(16)
    record.decision.status = "awaiting_approval"
    state.log(record.id, "approval", "Waiting for a person to approve or reject.")


def _plan_lines(plan: Plan) -> list[tuple[str, int]]:
    return [(line.offer.product_id, line.quantity) for line in plan.lines]


def _plan_needs_reapproval(approved: Plan | None, current: Plan) -> bool:
    """True when the live plan is no longer what the person signed off on.

    Fresh offers with the same products/qty and a total that did not rise keep the
    prior approval (so an expired JWT refresh does not bounce back to the person).
    """
    if approved is None:
        return True
    if _plan_lines(approved) != _plan_lines(current):
        return True
    return current.total > approved.total


def _replan(
    record: RequestRecord,
    change: str,
    *,
    approved_by_person: bool = False,
    approved_plan: Plan | None = None,
) -> None:
    """Exception 4: something moved between the decision and the order."""
    record.replans += 1
    state.log(record.id, "recheck", f"Re-check just before ordering: {change} No order was placed; planning again with fresh offers.")
    if record.replans > MAX_REPLANS:
        fail(record, "recheck", f"Prices or stock changed {record.replans} times in a row while ordering.")
        return
    try:
        record.offers = lookup_step.refresh(record.request, record.candidate_ids)
    except (GatewayError, GatewayRefused) as error:
        fail(record, "recheck", f"The supplier could not answer. {error}")
        return
    decide(record, approved_by_person=approved_by_person, approved_plan=approved_plan)


def act(
    record: RequestRecord,
    approved_by_person: bool = False,
    approved_plan: Plan | None = None,
) -> None:
    """Step 7: order, ask a person, or stop. Ordering always starts with a live re-check."""
    decision = record.decision
    if decision.policy_result.outcome == "deny":
        decision.status, decision.message = "rejected", f"Stopped by the rules. {NOTHING_ORDERED}"
        state.log(record.id, "order", decision.message)
        return
    if decision.policy_result.outcome == "needs_approval":
        # A person already signed off: only ask again when the live cart itself changed
        # (products, qty, or a higher total). Offer JWT refresh alone must not bounce back —
        # that redraw was landing a second click on Reject.
        if not approved_by_person:
            return _wait_for_person(record)
        if approved_plan is not None and _plan_needs_reapproval(approved_plan, decision.chosen_plan):
            state.log(
                record.id, "approval",
                "The refreshed plan differs from what was approved, so a person must sign off again.",
            )
            return _wait_for_person(record)
    try:
        confirmation = ordering.place(record.request, decision.chosen_plan, state.policy, approved_by_person)
    except ordering.Replan as change:
        # Keep the plan the person already approved across a refresh / price-check replan.
        snapshot = approved_plan or (decision.chosen_plan if approved_by_person else None)
        return _replan(
            record, str(change),
            approved_by_person=approved_by_person, approved_plan=snapshot,
        )
    except ordering.TrustBlocked as blocked:
        decision.policy_result = PolicyResult(outcome="deny", reasons=[str(blocked)])
        decision.status, decision.message = "rejected", f"Stopped by trust checks. {NOTHING_ORDERED}"
        state.log(record.id, "order", f"Refused before ordering: {blocked} It was not used.")
        return
    except ordering.SupplierWantsAPerson as refusal:
        reason = f"Supplier's check: Valio's gateway did not accept the agent's standing authority for this order ({refusal})."
        decision.policy_result = PolicyResult(outcome="needs_approval", reasons=[reason])
        state.log(record.id, "order", f"{reason} No order was placed.")
        return _wait_for_person(record)
    except ordering.PaymentFailed as error:
        return fail(record, "order", f"Payment failed. {error}")
    except (GatewayError, GatewayRefused) as error:
        return fail(record, "order", f"The order could not be placed. {error}")
    decision.order_confirmation, decision.status = confirmation, "ordered"
    who = "approved and signed by a person" if confirmation.flow == "human_present" else "signed by the agent within its standing limits"
    receipts = (
        "Receipt verified: signed by Valio's key and quotes the same offer and approval."
        if confirmation.receipts_verified else "Receipts were not re-checked."
    )
    paid = f" Paid with {confirmation.payment_method}." if confirmation.payment_method else ""
    state.log(
        record.id, "order",
        "Checked the offer again before ordering: signature valid, order matches the offer. "
        f"Order {confirmation.order_id} placed for {confirmation.total:.2f} EUR, {who} (AP2).{paid} {receipts}",
        confirmation=confirmation.model_dump(mode="json"),
    )
    # Track units on order so IP stays above R until the camera sees the delivery.
    if record.product_key and record.request is not None:
        with state.lock:
            row = state.shelf.get(record.product_key)
            if row is not None:
                qty = float(record.request.quantity)
                row.on_order_units = float(row.on_order_units or 0) + qty
                row.on_order_since = state.now()
                lead = row.lead_time_days if row.lead_time_days is not None else 2
                row.on_order_lead_days = float(lead)


def finalise(record: RequestRecord) -> None:
    """Step 8: the closing entry. The Decision itself already lives in the state."""
    endings = {
        "ordered": "Finished: the order is placed.", "awaiting_approval": "Paused: waiting for a person.",
        "needs_clarification": "Paused: waiting for an answer to the question.",
        "rejected": f"Finished: rejected. {NOTHING_ORDERED}", "failed": f"Finished: failed. {NOTHING_ORDERED}",
    }
    state.log(record.id, "finalise", endings.get(record.decision.status, f"Finished with status {record.decision.status}."),
              decision=state.without_signatures(record.decision.model_dump(mode="json")))
    if record.product_key and record.decision.status == "failed":
        # A failed run should not silence the shelf: the next "low" event may try again.
        state.shelf[record.product_key].armed = True


def decide(
    record: RequestRecord,
    *,
    approved_by_person: bool = False,
    approved_plan: Plan | None = None,
) -> None:
    """Steps 3 to 7. Also run again after a re-check finds a change."""
    plans = build_plans(record)
    if plans is None:
        return
    remaining, findings, removed = web_checks(record, plans.plans)
    choose_and_explain(record, remaining, findings, removed)
    policy_check(record)
    act(record, approved_by_person=approved_by_person, approved_plan=approved_plan)
