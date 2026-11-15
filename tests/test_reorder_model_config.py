"""R1: model clock helper and reorder-model config values (not wired into ordering yet)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app import config, forecast, settings, state


def test_model_clock_900_real_seconds_is_half_a_model_day(world, monkeypatch):
    """Spec §7: 900 real seconds at model_day_seconds=1800 → 0.5 model days."""
    settings.SHELF_FILE.write_text(
        "reorder_model:\n  model_day_seconds: 1800\ndefaults:\n  reorder_quantity: 24\n",
        encoding="utf-8",
    )
    assert config.load_reorder_model().model_day_seconds == 1800
    start = state.now()
    monkeypatch.setattr(state, "now", lambda: start)
    assert forecast.model_now() == start
    later = start + timedelta(seconds=900)
    assert abs(forecast.model_days_between(start, later) - 0.5) < 0.01


def test_model_clock_follows_config_override(world):
    """Changing model_day_seconds in shelf.yaml changes the scale (real-time = 86400)."""
    settings.SHELF_FILE.write_text(
        "reorder_model:\n  model_day_seconds: 86400\ndefaults:\n  reorder_quantity: 24\n",
        encoding="utf-8",
    )
    assert config.load_reorder_model().model_day_seconds == 86400
    start = state.now()
    # One real hour at real-time scale = 1/24 model days.
    assert abs(forecast.model_days_between(start, start + timedelta(hours=1)) - (1 / 24)) < 0.01


def test_reorder_model_globals_load_with_spec_defaults(world):
    """Repo shelf.yaml values; if the test fixture omits reorder_model, built-ins match the spec."""
    # Restore the real project shelf so we assert the checked-in defaults.
    from pathlib import Path
    real = Path(__file__).resolve().parent.parent / "config" / "shelf.yaml"
    settings.SHELF_FILE.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
    model = config.load_reorder_model()
    assert model.model_day_seconds == 86400
    assert model.alpha == 0.3
    assert model.sigma_factor == 1.5
    assert model.default_cover_days == 7
    assert model.default_lead_days == 2
    assert model.multiplier_min == 0.7 and model.multiplier_max == 1.5
    assert model.extra_lead_days_min == 0 and model.extra_lead_days_max == 3
    assert abs(model.z_for("critical") - 2.326) < 0.001
    assert abs(model.z_for("standard") - 1.645) < 0.001
    assert abs(model.z_for("perishable") - 1.282) < 0.001
    assert model.z_for("unknown") == model.z_for("standard")


def test_per_product_reorder_fields_resolve_from_shelf_defaults(world):
    """New per-product fields come from ShelfDefaults when a food omits them."""
    foods = config.load_camera_foods()
    defaults = config.load_shelf_defaults()
    assert defaults.service_class == "standard"
    assert defaults.units_per_visible_item == Decimal(1)
    assert defaults.pack_size == Decimal(24)
    assert defaults.max_stock_units == Decimal(144)
    assert defaults.shelf_life_days is None
    fields = config.resolved_reorder(foods["mystery_soda"], defaults)
    assert fields["service_class"] == "standard"
    assert fields["units_per_visible_item"] == Decimal(1)
    assert fields["pack_size"] == Decimal(24)
    assert fields["max_stock_units"] == Decimal(144)
    assert fields["shelf_life_days"] is None


def test_missing_reorder_model_section_uses_built_in_defaults(world):
    """An old shelf.yaml without reorder_model still works."""
    settings.SHELF_FILE.write_text("defaults:\n  reorder_quantity: 24\n", encoding="utf-8")
    model = config.load_reorder_model()
    assert model.model_day_seconds == 86400
    assert model.alpha == 0.3
    assert abs(model.z_for("standard") - 1.645) < 0.001
