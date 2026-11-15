"""R3/R4: usage updates, deliveries, and IP≤R trigger on piece-counted camera rows."""

from __future__ import annotations

from app import config, forecast, settings, state, vision


PCS_SHELF = """
reorder_model:
  model_day_seconds: 1800
  alpha: 0.3
  sigma_factor: 1.5
  z: {critical: 2.326, standard: 1.645, perishable: 1.282}
  default_cover_days: 7
  default_lead_days: 2
defaults:
  reorder_quantity: 24
  reorder_below: 2
  unit: pcs
  usual_quantity: 24
  lead_time_days: 2
  cover_days: 7
  typical_daily_use: 8
  service_class: standard
  units_per_visible_item: 24
  pack_size: 24
  max_stock_units: 144
"""


def _load_pcs_shelf():
    settings.SHELF_FILE.write_text(PCS_SHELF, encoding="utf-8")
    state.load()
    row = state.shelf["milk_lactose_free"]
    row.armed = True
    forecast.seed_usage(row)
    return row


def _foods():
    return {"milk_lactose_free": config.load_camera_foods()["milk_lactose_free"]}


def _found(count: int):
    return vision.to_detections(
        {"detections": [{"product_key": "milk_lactose_free", "box_2d": [100, 100, 400, 400],
                         "count": count, "confidence": 0.9}]},
        {"milk_lactose_free": "Milk"},
        state.now(),
    )


def test_stable_drop_updates_usage_rate(world):
    row = _load_pcs_shelf()
    row.quantity = 3.0
    row.usage_updated_at = state.now()
    when = state.now()
    for _ in range(vision.COUNT_AGREE):
        vision.apply_shelf_counts(_found(2), _foods(), when)
    assert row.unit == "pcs"
    assert row.usage_observations == 1
    assert row.usage_rate_d is not None and row.usage_rate_d > 8


def test_stable_rise_reduces_on_order_not_usage(world):
    row = _load_pcs_shelf()
    row.quantity = 1.0
    row.on_order_units = 72.0
    before_d = row.usage_rate_d
    when = state.now()
    for _ in range(vision.COUNT_AGREE):
        vision.apply_shelf_counts(_found(4), _foods(), when)
    assert row.on_order_units == 0.0
    assert row.usage_rate_d == before_d


def test_ip_at_or_below_r_starts_reorder(world):
    row = _load_pcs_shelf()
    row.quantity = None
    when = state.now()
    records = []
    for _ in range(vision.COUNT_AGREE + vision.CONFIRM_LOOKS):
        records.extend(vision.apply_shelf_counts(_found(1), _foods(), when))
    assert any(r.product_key == "milk_lactose_free" for r in records)
    assert row.reorder_why and "reordering" in row.reorder_why
