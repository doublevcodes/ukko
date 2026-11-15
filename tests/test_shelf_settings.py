"""Per-item reorder-below and order-quantity settings from the inventory table."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app import config, state
from app.main import app


def test_put_shelf_saves_reorder_settings_and_updates_live_row(world):
    client = TestClient(app)
    response = client.put("/shelf/milk_lactose_free", json={"reorder_below": 3, "reorder_quantity": 48})
    assert response.status_code == 200
    body = response.json()["shelf"]
    assert body["reorder_below"] == "3" and body["reorder_quantity"] == "48"
    assert body["usual_quantity"] == "48"

    row = state.shelf["milk_lactose_free"]
    assert row.reorder_below == Decimal(3) and row.reorder_quantity == Decimal(48)

    food = config.load_camera_foods()["milk_lactose_free"]
    assert food.reorder_below == Decimal(3) and food.reorder_quantity == Decimal(48)


def test_put_shelf_rejects_unknown_product(world):
    client = TestClient(app)
    assert client.put("/shelf/no_such_item", json={"reorder_below": 1, "reorder_quantity": 12}).status_code == 404


def test_put_shelf_rejects_zero_order_quantity(world):
    client = TestClient(app)
    assert client.put("/shelf/milk_lactose_free", json={"reorder_below": 1, "reorder_quantity": 0}).status_code == 422
