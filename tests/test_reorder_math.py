"""R2: pure reorder-model functions — worked examples from REORDER_SPEC.md §7."""

from __future__ import annotations

from datetime import timedelta

from app import forecast

TOL = 0.01


def test_reorder_point_standard():
    d_lead, d_eff, buf, r = forecast.reorder_point(8, 2, [1, 1], z=1.645, sigma_factor=1.5)
    assert abs(d_lead - 16) < TOL and abs(d_eff - 8) < TOL
    assert abs(buf - 9.87) < TOL and abs(r - 25.87) < TOL


def test_reorder_point_critical():
    _d, _e, buf, r = forecast.reorder_point(8, 2, [1, 1], z=2.326, sigma_factor=1.5)
    assert abs(buf - 13.96) < TOL and abs(r - 29.96) < TOL


def test_reorder_point_perishable():
    _d, _e, buf, r = forecast.reorder_point(8, 2, [1, 1], z=1.282, sigma_factor=1.5)
    assert abs(buf - 7.69) < TOL and abs(r - 23.69) < TOL


def test_reorder_point_with_demand_signals():
    d_lead, d_eff, buf, r = forecast.reorder_point(
        40, 2, [1.3, 0.925], z=1.645, sigma_factor=1.5,
    )
    assert abs(d_lead - 89.00) < TOL and abs(d_eff - 44.5) < TOL
    assert abs(buf - 23.28) < TOL and abs(r - 112.28) < TOL


def test_rate_update_after_drop():
    tau = forecast.tau_from_alpha(0.3)
    assert abs(tau - 2.804) < TOL
    d = forecast.update_usage_rate(d=8, units=24, dt_model_days=0.1, alpha=0.3)
    assert abs(d - 16.28) < TOL


def test_order_quantity_rounds_up_to_packs():
    size = forecast.order_quantity(
        d=8, lead_days=2, cover_days=7,
        lead_multipliers=[1, 1], cover_multipliers=[1] * 7,
        z=1.645, sigma_factor=1.5, ip=24, pack_size=24, max_stock_units=144,
    )
    assert abs(size.S - 81.87) < TOL
    assert abs(size.raw_Q - 57.87) < TOL
    assert abs(size.Q - 72) < TOL


def test_order_quantity_shelf_life_cap():
    size = forecast.order_quantity(
        d=8, lead_days=2, cover_days=7,
        lead_multipliers=[1, 1], cover_multipliers=[1] * 7,
        z=1.645, sigma_factor=1.5, ip=24, pack_size=24, max_stock_units=144,
        shelf_life_days=5,
    )
    assert abs(size.Q - 24) < TOL
    assert any("shelf life" in note for note in size.cap_notes)


def test_trigger_ip_above_reorder_point():
    assert forecast.should_reorder(48, 46.64) is False


def test_trigger_ip_at_or_below_reorder_point():
    assert forecast.should_reorder(24, 25.87) is True


def test_delivery_seen_clears_on_order():
    on_order = forecast.apply_delivery(on_order_units=72, visible_increase=3, units_per_visible_item=24)
    assert abs(on_order - 0) < TOL


def test_model_clock_half_day(world, monkeypatch):
    from app import settings, state
    settings.SHELF_FILE.write_text(
        "reorder_model:\n  model_day_seconds: 1800\ndefaults:\n  reorder_quantity: 24\n",
        encoding="utf-8",
    )
    start = state.now()
    assert abs(forecast.model_days_between(start, start + timedelta(seconds=900)) - 0.5) < TOL


def test_get_adjustments_stub_is_neutral():
    mults, extra, reasons = forecast.get_adjustments("any", 5)
    assert mults == [1.0] * 5 and extra == 0 and reasons == []
