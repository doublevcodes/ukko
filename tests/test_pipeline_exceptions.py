"""The nine exceptions from SPEC.md Section 15, plus the happy path.

Each test runs the real pipeline, the real gateway and real AP2 signatures.
Only Valio, the language model and Exa are faked (see conftest.py).
"""

from __future__ import annotations

from decimal import Decimal

from app import intake
from tests.helpers import approve, event, in_days, log_text, set_limit, shelf_runs_low, typed


def test_happy_path_agent_orders_alone(world):
    record = shelf_runs_low()
    decision = record.decision
    assert decision.status == "ordered"
    assert decision.chosen_plan.id == "A" and decision.chosen_plan.total == Decimal("200.00")
    assert decision.policy_result.outcome == "auto_approve"
    assert decision.order_confirmation.flow == "human_not_present"
    assert decision.order_confirmation.receipts_verified
    assert len(world.orders()) == 1


def test_1_partial_stock_gives_plan_b_and_needs_approval(world):
    world.valio.stock["1001"] = 60
    record = shelf_runs_low()
    plan = record.decision.chosen_plan
    assert record.decision.status == "awaiting_approval"
    assert plan.id == "B" and plan.coverage == 1
    assert [(line.offer.product_id, line.quantity, line.is_substitute) for line in plan.lines] == [("1001", 60, False), ("1002", 40, True)]
    assert any("Substitute rule" in reason for reason in record.decision.policy_result.reasons)
    assert record.decision.alternatives[0].plan.id == "A" and "60%" in record.decision.alternatives[0].why_not
    assert world.orders() == []


def test_2_nothing_on_time_gives_plan_c_and_needs_approval(world):
    world.valio.delivery_in_days = 10  # the shelf product is needed in 3 days
    record = shelf_runs_low()
    assert record.decision.status == "awaiting_approval"
    assert record.decision.chosen_plan.id == "C" and not record.decision.chosen_plan.meets_deadline
    assert any("Late delivery rule" in reason for reason in record.decision.policy_result.reasons)


def test_3_total_above_limit_needs_approval_then_person_signs(world):
    set_limit(150)
    record = shelf_runs_low()
    assert record.decision.status == "awaiting_approval"
    assert any("Spend limit rule" in reason for reason in record.decision.policy_result.reasons)
    approve(record)
    assert record.decision.status == "ordered"
    assert record.decision.order_confirmation.flow == "human_present"
    assert record.decision.order_confirmation.receipts_verified


def test_4_price_rises_before_an_approved_order_so_it_replans_and_asks_again(world):
    set_limit(150)
    record = shelf_runs_low()
    world.valio.prices["1001"] = 2.10
    approve(record)
    assert record.decision.status == "awaiting_approval"  # the person approved 200 EUR, not 210
    assert record.decision.chosen_plan.total == Decimal("210.00")
    assert "Re-check just before ordering" in log_text(record) and "rose from 2.00 to 2.10" in log_text(record)
    assert world.orders() == []
    approve(record)
    assert record.decision.status == "ordered" and record.decision.order_confirmation.total == Decimal("210.00")


def test_4_price_rises_while_the_agent_is_ordering_alone(world):
    def raise_price_at_checkout(call_number: int) -> None:
        if call_number == 3:  # calls 1 and 2 are the offers; call 3 starts the checkout
            world.valio.prices["1001"] = 2.10
    world.valio.on_call = raise_price_at_checkout
    record = shelf_runs_low()
    assert record.decision.status == "ordered" and record.replans == 1
    assert record.decision.order_confirmation.total == Decimal("210.00")
    assert "Re-check just before ordering" in log_text(record)


def test_4_stock_falls_before_the_order_so_it_replans(world):
    set_limit(150)
    record = shelf_runs_low()
    world.valio.stock["1001"] = 60
    approve(record)
    assert record.decision.status == "awaiting_approval" and record.decision.chosen_plan.id == "B"
    assert "Stock changed since the plan was made" in log_text(record)
    assert world.orders() == []


def test_5_valio_down_fails_clearly_and_orders_nothing(world):
    world.valio.down = True
    record = shelf_runs_low()
    assert record.decision.status == "failed"
    assert "Valio" in record.decision.message and "Nothing was ordered" in record.decision.message
    assert world.orders() == []


def test_5_valio_down_at_order_time_orders_nothing(world):
    set_limit(150)
    record = shelf_runs_low()
    world.valio.down = True
    approve(record)
    assert record.decision.status == "failed" and world.orders() == []


def test_6_exa_down_means_check_failed_and_needs_approval(world):
    world.exa.down = True
    record = shelf_runs_low()
    assert record.decision.status == "awaiting_approval"
    assert {finding.severity for finding in record.decision.web_findings} == {"check_failed"}
    assert any("Web check failed rule" in reason for reason in record.decision.policy_result.reasons)
    assert world.orders() == []


def test_7_duplicate_sensor_events_make_one_request(world):
    from app import state
    record = shelf_runs_low()
    assert intake.sensor_event(event())[0] == "ignored duplicate"  # already ordered, shelf not refilled
    assert len(state.requests) == 1 and len(world.orders()) == 1
    assert any("Ignored duplicate" in entry.summary for entry in state.audit)
    assert intake.sensor_event(event("full"))[0] == "level recorded"
    assert intake.sensor_event(event())[0] == "request created"  # refilled, then low again: a new request
    assert record.decision.status == "ordered"


def test_8_off_topic_text_gets_a_question_and_no_order(world):
    record = typed(world, "what's the weather like?", is_purchase_request=False)
    assert record.decision.status == "needs_clarification" and record.decision.message
    assert world.valio.calls == 0 and world.orders() == []


def test_8_vague_text_gets_a_question_not_a_guess(world):
    record = typed(world, "we need some milk", item_description="milk", search_terms="milk")
    assert record.decision.status == "needs_clarification"
    assert "how much" in record.decision.message and "by which date" in record.decision.message
    assert world.valio.calls == 0 and world.orders() == []


def test_8_two_products_in_one_message_are_not_merged(world):
    record = typed(world, "ten kilos of potatoes and ten kilos of onions by Friday", several_products=True,
                   item_description="potatoes and onions", search_terms="potatoes onions", quantity=20, unit="kg",
                   needed_by=in_days(3))
    assert record.decision.status == "needs_clarification"
    assert "one product per request" in record.decision.message
    assert world.valio.calls == 0 and world.orders() == []


def test_8_search_hits_that_are_not_the_item_are_not_ordered(world):
    world.llm.matches_nothing = True
    record = typed(world, "10 kg of butter by Friday", item_description="butter", search_terms="butter",
                   quantity=10, unit="kg", needed_by=in_days(3))
    assert record.decision.status == "needs_clarification"
    assert "found no product" in record.decision.message
    assert record.decision.chosen_plan is None and world.valio.calls == 0 and world.orders() == []


def test_9_person_rejects(world):
    set_limit(150)
    record = shelf_runs_low()
    assert intake.claim_approval(record)
    intake.reject(record, "too expensive", "test")
    assert record.decision.status == "rejected"
    assert "rejected the plan" in log_text(record) and "too expensive" in log_text(record)
    assert world.orders() == []
    assert not intake.claim_approval(record)  # the approval cannot be used afterwards
