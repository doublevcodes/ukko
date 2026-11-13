"""One test per policy rule. policy.evaluate is pure, so these need no fakes."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.models import Offer, OrderRequest, Plan, PlanLine, PolicyConfig, WebFinding
from app.policy import evaluate

TODAY = date.today()
DEADLINE = TODAY + timedelta(days=3)


def request(**changes) -> OrderRequest:
    base = dict(id="req-1", source="sensor", item_description="milk", quantity=Decimal(100), unit="l",
                needed_by=DEADLINE, constraints=["FREE_FROM_LACTOSE"], usual_quantity=Decimal(100))
    return OrderRequest(**(base | changes))


def offer(**changes) -> Offer:
    base = dict(product_id="1001", name="Arla milk 1 L lactose free", unit_price=Decimal("2.00"), pack_size=Decimal(1),
                pack_unit="L", earliest_delivery=TODAY + timedelta(days=1), attributes=["FREE_FROM_LACTOSE"],
                orderable=True, category="10400", category_name="dairy")
    return Offer(**(base | changes))


def plan(lines: list[PlanLine] | None = None, **changes) -> Plan:
    lines = lines or [PlanLine(offer=offer(), quantity=100, line_total=Decimal("200.00"), covers=Decimal(100))]
    base = dict(id="A", lines=lines, total=sum(line.line_total for line in lines), meets_deadline=True,
                meets_constraints=True, coverage=Decimal(1), delivery_date=TODAY + timedelta(days=1))
    return Plan(**(base | changes))


def finding(severity: str, check_type: str = "recall") -> WebFinding:
    return WebFinding(check_type=check_type, query="q", summary="Summary of the finding.", severity=severity)


def outcome_and_reasons(**kwargs) -> tuple[str, str]:
    result = evaluate(kwargs.get("request", request()), kwargs.get("plan", plan()),
                      kwargs.get("findings", []), kwargs.get("config", PolicyConfig()))
    return result.outcome, " ".join(result.reasons)


def test_auto_approve_when_every_rule_passes():
    outcome, reasons = outcome_and_reasons(findings=[finding("none")])
    assert outcome == "auto_approve" and "All rules passed" in reasons


def test_deny_when_a_hard_requirement_is_broken():
    line = PlanLine(offer=offer(attributes=[]), quantity=100, line_total=Decimal("200.00"), covers=Decimal(100))
    outcome, reasons = outcome_and_reasons(plan=plan([line]))
    assert outcome == "deny" and "Hard requirement rule" in reasons and "FREE_FROM_LACTOSE" in reasons


def test_deny_when_a_web_check_finds_a_blocker():
    outcome, reasons = outcome_and_reasons(findings=[finding("blocker")])
    assert outcome == "deny" and "Web blocker rule" in reasons


def test_deny_beats_needs_approval():
    outcome, _ = outcome_and_reasons(findings=[finding("blocker"), finding("caution")], config=PolicyConfig(auto_approve_max_total_eur=1))
    assert outcome == "deny"


def test_total_above_the_limit_needs_approval():
    outcome, reasons = outcome_and_reasons(config=PolicyConfig(auto_approve_max_total_eur=150))
    assert outcome == "needs_approval"
    assert "Spend limit rule: the total of 200.00 EUR is above the 150.00 EUR auto-approve limit." in reasons


def test_total_exactly_at_the_limit_is_allowed():
    outcome, _ = outcome_and_reasons(config=PolicyConfig(auto_approve_max_total_eur=200))
    assert outcome == "auto_approve"


def test_substitute_needs_approval_and_the_rule_can_be_switched_off():
    lines = [PlanLine(offer=offer(), quantity=100, line_total=Decimal("200.00"), covers=Decimal(100), is_substitute=True)]
    outcome, reasons = outcome_and_reasons(plan=plan(lines))
    assert outcome == "needs_approval" and "Substitute rule" in reasons
    outcome, _ = outcome_and_reasons(plan=plan(lines), config=PolicyConfig(substitutes_require_approval=False))
    assert outcome == "auto_approve"


def test_late_delivery_needs_approval_and_the_rule_can_be_switched_off():
    late = plan(meets_deadline=False, delivery_date=DEADLINE + timedelta(days=2))
    outcome, reasons = outcome_and_reasons(plan=late)
    assert outcome == "needs_approval" and "Late delivery rule" in reasons
    outcome, _ = outcome_and_reasons(plan=late, config=PolicyConfig(late_delivery_requires_approval=False))
    assert outcome == "auto_approve"


def test_web_caution_needs_approval_and_the_rule_can_be_switched_off():
    outcome, reasons = outcome_and_reasons(findings=[finding("caution", "price_sanity")])
    assert outcome == "needs_approval" and "Web caution rule: the price check" in reasons
    outcome, _ = outcome_and_reasons(findings=[finding("caution")], config=PolicyConfig(web_caution_requires_approval=False))
    assert outcome == "auto_approve"


def test_failed_web_check_needs_approval_and_the_rule_can_be_switched_off():
    failed = [finding("check_failed"), finding("check_failed", "price_sanity")]
    outcome, reasons = outcome_and_reasons(findings=failed)
    assert outcome == "needs_approval" and "Web check failed rule: 2 web check(s)" in reasons
    outcome, _ = outcome_and_reasons(findings=failed, config=PolicyConfig(web_check_failed_requires_approval=False))
    assert outcome == "auto_approve"


def test_unusually_large_quantity_needs_approval():
    outcome, reasons = outcome_and_reasons(request=request(quantity=Decimal(301)))
    assert outcome == "needs_approval" and "Unusual quantity rule: 301 l is more than 3 times the usual 100 l." in reasons
    assert outcome_and_reasons(request=request(quantity=Decimal(300)))[0] == "auto_approve"  # exactly 3 times is allowed
    assert outcome_and_reasons(request=request(quantity=Decimal(900), usual_quantity=None))[0] == "auto_approve"  # no usual amount known


def test_category_off_the_allowed_list_needs_approval():
    butter = [PlanLine(offer=offer(category="10500", category_name="butter and spreads"), quantity=100,
                       line_total=Decimal("200.00"), covers=Decimal(100))]
    outcome, reasons = outcome_and_reasons(plan=plan(butter))
    assert outcome == "needs_approval" and "Category rule" in reasons and "butter and spreads" in reasons


def test_unrecognised_category_needs_approval():
    unknown = [PlanLine(offer=offer(category="99999", category_name=None), quantity=100,
                        line_total=Decimal("200.00"), covers=Decimal(100))]
    outcome, reasons = outcome_and_reasons(plan=plan(unknown))
    assert outcome == "needs_approval" and "does not recognise" in reasons


def test_incomplete_order_needs_approval():
    outcome, reasons = outcome_and_reasons(plan=plan(coverage=Decimal("0.6")))
    assert outcome == "needs_approval" and "Incomplete order rule: the plan covers only 60%" in reasons
