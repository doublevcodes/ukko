"""The reorder amount follows measured usage and booked customer demand."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app import config, forecast, intake, state
from tests.helpers import event


def test_no_usage_measured_uses_the_shelf_setting_plus_bookings(world):
    product = config.load_shelf()["milk_lactose_free"]
    today = state.now().date()
    forecast.DEMAND_FILE.write_text(f"demand:\n  milk_lactose_free:\n  - date: {today + timedelta(days=2)}\n    quantity: 30\n"
                                    f"  - date: {today + timedelta(days=60)}\n    quantity: 999\n", encoding="utf-8")
    try:
        amount = forecast.reorder("milk_lactose_free", product, "low", today)
    finally:
        forecast.DEMAND_FILE.unlink()
    assert amount.daily_rate is None and amount.quantity == Decimal(130)  # 100 configured + 30 booked; day 60 is too far


def test_measured_usage_is_reported_but_order_stays_configured(world):
    product = config.load_shelf()["milk_lactose_free"]  # reorder_quantity = 100
    now = state.now()
    forecast.record_cycle("milk_lactose_free", product, now - timedelta(days=5), now, "low")  # 75 l in 5 days
    amount = forecast.reorder("milk_lactose_free", product, "low", now.date())
    assert amount.daily_rate == Decimal("15.0")
    assert amount.quantity == Decimal(100)  # configured reorder_quantity, not rate × cover


def test_sensor_cycle_is_recorded_and_order_uses_configured_quantity(world, monkeypatch):
    start = state.now()
    monkeypatch.setattr(state, "now", lambda: start)
    intake.sensor_event(event("full"))
    monkeypatch.setattr(state, "now", lambda: start + timedelta(days=2))
    result, record = intake.sensor_event(event("empty"))  # 100 l in 2 days
    assert result == "request created" and forecast.daily_rate("milk_lactose_free") == Decimal(50)
    intake.run_sensor(record)
    assert record.request.quantity == Decimal(100)  # configured reorder_quantity


def test_forecast_orders_before_the_shelf_runs_out(world, monkeypatch):
    state.shelf["milk_lactose_free"].typical_daily_use = Decimal(4)
    start = state.now()
    monkeypatch.setattr(state, "now", lambda: start)
    intake.sensor_event(event("full"))  # 100 l at the typical 4 l/day would last 25 days
    assert intake.check_forecasts() == []
    forecast.DEMAND_FILE.write_text(f"demand:\n  milk_lactose_free:\n  - date: {start.date() + timedelta(days=5)}\n"
                                    "    quantity: 90\n", encoding="utf-8")
    try:
        raised = intake.check_forecasts()
        assert len(raised) == 1 and "runs out" in raised[0].title
        assert intake.check_forecasts() == []  # one order per run-out, not one every check
        intake.run_sensor(raised[0])
    finally:
        forecast.DEMAND_FILE.unlink()
    assert raised[0].request.quantity == Decimal(190)  # 100 configured + 90 booked


def test_camera_foods_not_seen_stocked_do_not_order(world):
    for row in state.shelf.values():  # the camera looked and saw nothing, e.g. not in frame
        row.level, row.quantity, row.updated_at = "empty", 0, state.now()
    assert intake.check_forecasts() == []
