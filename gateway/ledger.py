"""What the supplier remembers between requests.

- Registered buyers and the public key each one signs approvals with.
- Placed orders, so the same request can never be ordered twice.
- How much has been spent under each standing authority (AP2 open mandate).

Buyers and orders are also written to disk, so a restart forgets nothing.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ap2.sdk.constraints import MandateContext
from jwcrypto.jwk import JWK

from gateway import settings

HELSINKI = ZoneInfo("Europe/Helsinki")
_lock = threading.Lock()
_buyers: dict[str, dict[str, Any]] = {}
_orders: dict[str, dict[str, Any]] = {}
_order_by_request: dict[tuple[str, str], str] = {}


def _buyers_file():
    return settings.GATEWAY_DATA_DIR / "buyers.json"


def _orders_file():
    return settings.GATEWAY_DATA_DIR / "orders.jsonl"


def load() -> None:
    """Read buyers and orders back from disk (called once at startup)."""
    with _lock:
        _buyers.clear(), _orders.clear(), _order_by_request.clear()
        if _buyers_file().exists():
            _buyers.update(json.loads(_buyers_file().read_text(encoding="utf-8")))
        if _orders_file().exists():
            for line in _orders_file().read_text(encoding="utf-8").splitlines():
                order = json.loads(line)
                _orders[order["order_id"]] = order
                _order_by_request[(order["buyer_id"], order["request_id"])] = order["order_id"]


def register_buyer(buyer_id: str, public_jwk: dict[str, Any]) -> bool:
    """Remember a buyer's approval key. Returns True if the buyer is new.

    A stand-in for customer onboarding. The first key registered for a buyer
    wins: a different key for the same buyer is refused.
    """
    JWK(**public_jwk)  # raises if this is not a valid key
    if "d" in public_jwk:
        raise ValueError("Send only the public key, never the private part.")
    with _lock:
        known = _buyers.get(buyer_id)
        if known is not None and known != public_jwk:
            raise ValueError(
                f"Buyer '{buyer_id}' is already registered with a different key. "
                f"To start over, stop the gateway and delete {_buyers_file()}."
            )
        _buyers[buyer_id] = public_jwk
        _buyers_file().parent.mkdir(parents=True, exist_ok=True)
        path = _buyers_file()
        path.write_text(json.dumps(_buyers, indent=2), encoding="utf-8")
        try:
            from app import durable

            durable.mirror(path)
        except Exception:  # noqa: BLE001
            pass
        return known is None


def buyer_key(buyer_id: str) -> JWK | None:
    with _lock:
        jwk = _buyers.get(buyer_id)
    return JWK(**jwk) if jwk else None


def find_order(buyer_id: str, request_id: str) -> dict[str, Any] | None:
    """The order already placed for this buyer's request, if any."""
    with _lock:
        order_id = _order_by_request.get((buyer_id, request_id))
        return _orders.get(order_id) if order_id else None


def get_order(order_id: str) -> dict[str, Any] | None:
    with _lock:
        return _orders.get(order_id)


def all_orders() -> list[dict[str, Any]]:
    with _lock:
        return list(_orders.values())[::-1]


def record_order(order: dict[str, Any], receipts: Callable[[str], dict[str, str]]) -> dict[str, Any]:
    """Store a new order under a fresh order number and return it.

    `receipts` is given the new order number and returns the signed receipts
    to store with the order. If this buyer's request already has an order,
    that order is returned instead: the check and the write happen under one
    lock, so two calls arriving together still produce a single order.
    """
    with _lock:
        existing = _order_by_request.get((order["buyer_id"], order["request_id"]))
        if existing:
            return _orders[existing]
        order_id = f"VA-{datetime.now(HELSINKI):%Y%m%d}-{len(_orders) + 1:04d}"
        order = {"order_id": order_id, **order, **receipts(order_id)}
        _orders[order["order_id"]] = order
        _order_by_request[(order["buyer_id"], order["request_id"])] = order["order_id"]
        path = _orders_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(order, ensure_ascii=False) + "\n")
        try:
            from app import durable

            durable.mirror(path)
        except Exception:  # noqa: BLE001
            pass
        return order


def usage(standing_mandate: str) -> MandateContext:
    """Total spent so far under one standing authority, for AP2 budget checks."""
    with _lock:
        used = [o for o in _orders.values() if o.get("standing_mandate") == standing_mandate]
    return MandateContext(
        total_amount=sum(o["total_cents"] for o in used),
        total_uses=len(used),
    )
