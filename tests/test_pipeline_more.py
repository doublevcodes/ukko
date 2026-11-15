"""More pipeline behaviour: the supplier's own guard, typed requests, web-check rules."""

from __future__ import annotations

from decimal import Decimal

from app import authority, ordering, state
from app.models import PolicyConfig
from app.web import exa_checks
from tests.helpers import approve, in_days, log_text, shelf_runs_low, typed

RECALL_NOTICE = [{"title": "Recall: Arla semi skimmed milk", "url": "https://example.org/recall",
                  "published_date": "2026-11-01", "text": "The authority recalls Arla semi skimmed milk 1 L lactose free."}]


def test_gateway_refuses_the_agents_mandate_and_a_person_is_asked(world, monkeypatch):
    """Stockpot's own rules say "go", but the limits the supplier holds are lower: the supplier wins."""
    stale = authority.renew(PolicyConfig(auto_approve_max_total_eur=100))
    monkeypatch.setattr(authority, "ensure", lambda _policy: stale)
    record = shelf_runs_low()  # 200 EUR: fine for the 500 EUR policy, above the signed 100 EUR
    assert record.decision.status == "awaiting_approval"
    assert "Supplier's check" in record.decision.policy_result.reasons[0]
    assert "exceeds maximum" in record.decision.policy_result.reasons[0]
    assert world.orders() == []
    approve(record)
    assert record.decision.status == "ordered" and record.decision.order_confirmation.flow == "human_present"


def test_same_request_never_creates_two_orders(world):
    record = shelf_runs_low()
    again = ordering.place(record.request, record.decision.chosen_plan, state.policy, approved_by_person=False)
    assert again.order_id == record.decision.order_confirmation.order_id
    assert len(world.orders()) == 1


def test_agent_budget_is_enforced_by_the_supplier(world):
    state.policy = state.policy.model_copy(update={"agent_budget_eur": Decimal(300)})
    first = shelf_runs_low()  # 200 EUR of the 300 EUR budget
    assert first.decision.status == "ordered"
    second = typed(world, "100 litres of lactose-free semi skimmed milk", item_description="semi skimmed milk",
                   search_terms="semi skimmed milk", quantity=100, unit="l", needed_by=in_days(3), constraints=["FREE_FROM_LACTOSE"])
    assert second.decision.status == "awaiting_approval"  # 150 EUR more would pass 300 EUR
    assert "Supplier's check" in second.decision.policy_result.reasons[0]


def test_typed_request_prefers_organic_and_flags_the_category(world):
    record = typed(world, "20 kg of butter by Friday, organic if possible", item_description="butter", search_terms="butter",
                   quantity=20, unit="kg", needed_by=in_days(3), preferences=["EU_ORGANIC_FARMING"])
    plan = record.decision.chosen_plan
    assert plan.lines[0].offer.product_id == "2002" and plan.lines[0].quantity == 40  # 40 x 500 g
    assert plan.total == Decimal("240.00")
    assert record.decision.status == "awaiting_approval"
    assert any("Category rule" in reason and "butter and spreads" in reason for reason in record.decision.policy_result.reasons)


def test_hard_requirement_is_never_broken(world):
    record = typed(world, "50 l lactose-free semi skimmed milk", item_description="semi skimmed milk", search_terms="semi skimmed milk",
                   quantity=50, unit="l", needed_by=in_days(3), constraints=["FREE_FROM_LACTOSE"])
    # Exact match is by kind / plainness, not lowest price (1001 and 1002 are both lactose-free semi-skimmed).
    assert record.decision.chosen_plan.lines[0].offer.product_id in {"1001", "1002"}
    assert all("FREE_FROM_LACTOSE" in line.offer.attributes for line in record.decision.chosen_plan.lines)


def test_unknown_hard_requirement_gets_a_question(world):
    record = typed(world, "50 l of halal milk by Monday", item_description="milk", search_terms="milk", quantity=50, unit="l",
                   needed_by=in_days(3), unknown_hard_requirements=["halal"])
    assert record.decision.status == "needs_clarification" and "halal" in record.decision.message
    assert world.valio.calls == 0


def test_language_model_down_typed_request_fails_but_sensor_still_works(world):
    world.llm.down = True
    record = typed(world, "100 l of milk by Monday")
    assert record.decision.status == "failed" and world.orders() == []
    record = shelf_runs_low()
    assert record.decision.status == "ordered"  # the explanation falls back to a template
    assert "template" in log_text(record) and record.decision.explanation.startswith("Plan A orders")


def test_recall_blocker_is_denied_by_policy(world):
    world.exa.results = RECALL_NOTICE
    world.llm.verdict = {"verdict": "serious", "summary": "An official recall names this product.", "source_numbers": [1]}
    record = shelf_runs_low()
    assert record.decision.policy_result.outcome == "deny"
    assert record.decision.status == "rejected" and world.orders() == []
    recall = next(f for f in record.decision.web_findings if f.check_type == "recall")
    assert recall.severity == "blocker" and recall.sources[0].url == "https://example.org/recall"


def test_warning_without_a_source_is_not_accepted(world):
    world.exa.results = RECALL_NOTICE
    world.llm.verdict = {"verdict": "concern", "summary": "Trust me, there is a problem.", "source_numbers": []}
    finding = exa_checks.recall_check("Arla semi skimmed milk 1 L lactose free", "Arla")
    assert finding.severity == "check_failed" and finding.sources == []


def test_nothing_found_is_said_plainly_and_the_search_is_recorded(world):
    finding = exa_checks.recall_check("Arla semi skimmed milk 1 L lactose free", "Arla")
    assert finding.severity == "none" and finding.summary.startswith("No recent recall notices found")
    assert finding.query and world.exa.queries == [finding.query]
    exa_checks.recall_check("Arla semi skimmed milk 1 L lactose free", "Arla")
    assert len(world.exa.queries) == 1  # the second call came from the 10-minute cache
