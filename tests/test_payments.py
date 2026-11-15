"""Stripe test-mode: saved card charged on every order; failures order nothing."""

from __future__ import annotations

from app import payments
from tests.helpers import approve, log_text, set_limit, shelf_runs_low


def test_happy_path_charges_stripe_and_records_payment_method(world):
    record = shelf_runs_low()
    assert record.decision.status == "ordered"
    assert len(world.stripe.charges) == 1
    assert world.stripe.charges[0]["amount_cents"] == 20000
    assert world.orders()[0]["payment_method"].startswith("Stripe test · Visa")
    assert world.orders()[0]["stripe_payment_intent_id"] == "pi_test_1"
    assert "Paid with Stripe test" in log_text(record)


def test_human_approve_also_charges_stripe(world):
    set_limit(150)
    record = shelf_runs_low()
    assert record.decision.status == "awaiting_approval"
    assert world.stripe.charges == []
    approve(record)
    assert record.decision.status == "ordered"
    assert len(world.stripe.charges) == 1
    assert record.decision.order_confirmation.payment_method.startswith("Stripe test · Visa")


def test_missing_card_orders_nothing(world):
    payments.clear_profile()
    record = shelf_runs_low()
    assert record.decision.status == "failed"
    assert "No kitchen card" in record.decision.message
    assert world.orders() == []
    assert world.stripe.charges == []


def test_declined_card_orders_nothing(world):
    world.stripe.decline = True
    record = shelf_runs_low()
    assert record.decision.status == "failed"
    assert "declined" in record.decision.message.lower()
    assert world.orders() == []


def test_setup_session_and_finish_save_the_card(world, monkeypatch):
    payments.clear_profile()
    monkeypatch.setattr(payments.settings, "STRIPE_PUBLISHABLE_KEY", "pk_test_fake")
    session = payments.create_setup_session()
    assert session["session_id"].startswith("cs_test_")
    assert session["client_secret"].startswith("cs_test_secret_")
    status = payments.finish_setup(session["session_id"])
    assert status["ready"] and status["last4"] == "4242"
    assert payments.status()["label"] == "Visa ····4242"
