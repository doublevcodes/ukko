"""The rules that decide whether the agent may order on its own.

Ordinary code: no language model, no network. Given the same inputs it always
gives the same answer, and every reason is a sentence a kitchen manager can
read. The settings come from config/policy.yaml (editable in the dashboard).
"""

from __future__ import annotations

from decimal import Decimal

from app.models import OrderRequest, Plan, PolicyConfig, PolicyResult, WebFinding

CHECK_NAMES = {
    "recall": "recall check", "substitute_suitability": "substitute check",
    "price_sanity": "price check", "other": "web check",
}


def _number(value: Decimal) -> str:
    """Write 400 as "400" and 2.5 as "2.5", without trailing zeros."""
    return f"{value.normalize():f}"


def _deny_reasons(request: OrderRequest, plan: Plan, findings: list[WebFinding]) -> list[str]:
    reasons = []
    for line in plan.lines:
        facts = set(line.offer.attributes) | set(line.offer.labels)
        missing = [need for need in request.constraints if need not in facts]
        if missing:
            reasons.append(f"Hard requirement rule: {line.offer.name} is not marked {', '.join(missing)} in the catalogue.")
    if not plan.meets_constraints and not reasons:
        reasons.append("Hard requirement rule: this plan does not meet every hard requirement of the request.")
    for finding in findings:
        if finding.severity == "blocker":
            reasons.append(f"Web blocker rule: the {CHECK_NAMES[finding.check_type]} found a serious problem. {finding.summary}")
    return reasons


def _order_reasons(request: OrderRequest, plan: Plan, config: PolicyConfig) -> list[str]:
    """Rules about the order itself: size, substitutes, delivery, category."""
    reasons = []
    if request.source == "typed":
        reasons.append("Manual request rule: typed requests always need a person's approval.")
    if plan.total > config.auto_approve_max_total_eur:
        reasons.append(f"Spend limit rule: the total of {plan.total:.2f} EUR is above the "
                       f"{config.auto_approve_max_total_eur:.2f} EUR auto-approve limit.")
    substitutes = [line.offer.name for line in plan.lines if line.is_substitute]
    if substitutes and config.substitutes_require_approval:
        reasons.append(f"Substitute rule: the plan includes a substitute ({', '.join(substitutes)}).")
    if not plan.meets_deadline and config.late_delivery_requires_approval:
        arrives = f"on {plan.delivery_date:%a %d %b}" if plan.delivery_date else "late"
        reasons.append(f"Late delivery rule: the order would arrive {arrives}, after the {request.needed_by:%a %d %b} deadline.")
    if plan.coverage < 1:
        reasons.append(f"Incomplete order rule: the plan covers only {plan.coverage:.0%} of the requested quantity.")
    usual = request.usual_quantity
    if usual and request.quantity > usual * config.max_quantity_multiplier_vs_usual:
        reasons.append(
            f"Unusual quantity rule: {_number(request.quantity)} {request.unit} is more than "
            f"{_number(config.max_quantity_multiplier_vs_usual)} times the usual {_number(usual)} {request.unit}."
        )
    for line in plan.lines:
        group = line.offer.category_name
        if group not in config.allowed_categories:
            described = f"in the '{group}' group" if group else "in a product group Ukko does not recognise"
            reasons.append(f"Category rule: {line.offer.name} is {described}, which is not on the list "
                           f"the agent may buy on its own ({', '.join(config.allowed_categories) or 'empty'}).")
    return reasons


def _web_reasons(findings: list[WebFinding], config: PolicyConfig) -> list[str]:
    reasons = []
    if config.web_caution_requires_approval:
        reasons += [f"Web caution rule: the {CHECK_NAMES[finding.check_type]} raised a concern. {finding.summary}"
                    for finding in findings if finding.severity == "caution"]
    failed = [finding for finding in findings if finding.severity == "check_failed"]
    if failed and config.web_check_failed_requires_approval:
        # One sentence however many checks failed: they usually fail together, for one cause.
        names = ", ".join(dict.fromkeys(CHECK_NAMES[finding.check_type] for finding in failed))
        reasons.append(f"Web check failed rule: {len(failed)} web check(s) could not be completed ({names}), "
                       f"so a person should look. {failed[0].summary}")
    return reasons


def _trust_reasons(plan: Plan) -> tuple[list[str], list[str]]:
    """Trust-protocol policy additions: bad signature/issuer/audience block; origin needs a person."""
    denied: list[str] = []
    needs_person: list[str] = []
    for line in plan.lines:
        for check in line.offer.trust_checks or []:
            if check.get("passed"):
                continue
            name = check.get("name")
            reason = check.get("reason") or "Trust check failed."
            if name in {"signature", "supplier", "audience"}:
                denied.append(f"Trust rule: {reason}")
            elif name == "origin":
                needs_person.append(f"Trust rule: {reason}")
    return denied, needs_person


def evaluate(request: OrderRequest, plan: Plan, findings: list[WebFinding], config: PolicyConfig) -> PolicyResult:
    """Decide: order automatically, ask a person, or refuse."""
    denied = _deny_reasons(request, plan, findings)
    trust_denied, trust_person = _trust_reasons(plan)
    denied += trust_denied
    if denied:
        return PolicyResult(outcome="deny", reasons=denied)
    needs_person = _order_reasons(request, plan, config) + _web_reasons(findings, config) + trust_person
    if needs_person:
        return PolicyResult(outcome="needs_approval", reasons=needs_person)
    # Worded carefully: a rule may be switched off, so do not claim "no substitutes" or "no concerns".
    passed = (f"All rules passed: the total of {plan.total:.2f} EUR is within the "
              f"{config.auto_approve_max_total_eur:.2f} EUR limit, and none of the rules that are "
              "switched on asks for a person.")
    return PolicyResult(outcome="auto_approve", reasons=[passed])
