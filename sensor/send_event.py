"""The one function the shelf camera app calls to report a stock level.

Depends only on `requests`, so it can be copied onto the UNO Q on its own.
It never raises: a network problem must not crash the camera app.
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

TIMEOUT_SECONDS = 3


def send_stock_level(product_key: str, level: str, confidence: float, backend_url: str,
                     device_id: str = "uno-q-1") -> dict | None:
    """Tell Ukko that a shelf product is "full", "low" or "empty".

    product_key  a key from config/shelf.yaml, for example "milk_lactose_free"
    confidence   how sure the on-board model is, from 0 to 1
    backend_url  for example "http://192.168.1.20:8000"

    Returns Ukko's answer as a dict, or None if it could not be reached.
    """
    event = {
        "product_key": product_key, "level": level, "confidence": confidence,
        "timestamp": datetime.now(timezone.utc).isoformat(), "device_id": device_id,
    }
    try:
        response = requests.post(f"{backend_url.rstrip('/')}/events/sensor", json=event, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()
    except Exception as error:  # noqa: BLE001  by design: report and carry on
        print(f"send_stock_level: could not reach Ukko ({type(error).__name__}: {error})")
        return None
