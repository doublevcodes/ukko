"""How much to reorder: the average rate the shelf empties, plus what customers have booked.

The sensor only reports full, low or empty, so usage is measured per cycle:
from the moment the shelf is seen full after a refill until it is next seen
low or empty, the kitchen used roughly `full_quantity - what is left`. The
average rate is the total used over the total time, across the latest cycles,
so a long slow cycle counts for more than a short one.

Future customer requirements (bookings, events, catering) come from
config/demand.yaml and are added on top of the usual usage for the days the
order has to cover.

The order covers the days until it arrives plus `cover_days` after that,
minus what is still on the shelf. Without any measured cycle yet, the fixed
`typical_daily_use` from shelf.yaml stands in for the rate; without that
either, the fixed `reorder_quantity` is used, plus any booked demand.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Protocol

import yaml
from pydantic import BaseModel

from app import settings
from app.models import Level, Unit

CYCLES_KEPT = 8  # older cycles say little about today's menu
SHORTEST_CYCLE = timedelta(hours=1)  # quicker than this is a restock glitch, not usage
DEFAULT_LOW_FRACTION = Decimal("0.25")  # how full "low" is, when shelf.yaml does not say
RUNOUT_WINDOW_DAYS = 7  # the agent orders before the forecast shows the shelf running out within this many days
USAGE_FILE = settings.DATA_DIR / "usage_cycles.json"
DEMAND_FILE = settings.CONFIG_DIR / "demand.yaml"


class Stocked(Protocol):
    """The reorder settings the forecast needs: a shelf.yaml product or a camera shelf row."""

    reorder_quantity: Decimal | None
    unit: Unit | None
    lead_time_days: int | None
    full_quantity: Decimal | None
    low_quantity: Decimal | None
    cover_days: int
    typical_daily_use: Decimal | None


class Cycle(BaseModel):
    """One measured stretch from refilled to low or empty."""

    started: datetime
    ended: datetime
    used: Decimal  # in the product's unit


class Booking(BaseModel):
    """A known future need from config/demand.yaml."""

    date: date
    quantity: Decimal
    note: str = ""


class Forecast(BaseModel):
    quantity: Decimal
    daily_rate: Decimal | None  # None when no cycle has been measured yet
    booked: Decimal
    on_shelf: Decimal
    explanation: str


def _load_cycles() -> dict[str, list[Cycle]]:
    if not USAGE_FILE.exists():
        return {}
    raw = json.loads(USAGE_FILE.read_text(encoding="utf-8") or "{}")
    return {key: [Cycle(**cycle) for cycle in cycles] for key, cycles in raw.items()}


def _save_cycles(cycles: dict[str, list[Cycle]]) -> None:
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw = {key: [cycle.model_dump(mode="json") for cycle in items] for key, items in cycles.items()}
    USAGE_FILE.write_text(json.dumps(raw, indent=1), encoding="utf-8")


def full_amount(product: Stocked) -> Decimal:
    return product.full_quantity if product.full_quantity is not None else (product.reorder_quantity or Decimal(0))


def _lead(product: Stocked) -> int:
    return product.lead_time_days if product.lead_time_days is not None else 2


def left_at(level: Level, product: Stocked) -> Decimal:
    """Roughly how much is still on the shelf at this level."""
    if level == "full":
        return full_amount(product)
    if level == "empty":
        return Decimal(0)
    return product.low_quantity if product.low_quantity is not None else full_amount(product) * DEFAULT_LOW_FRACTION


def record_cycle(product_key: str, product: Stocked, started: datetime, ended: datetime, level: Level,
                 used: Decimal | None = None) -> Cycle | None:
    """Save one full-to-low (or empty) stretch. Returns it, or None when it was too short to count.

    `used` is the counted amount when the camera saw how many units went; otherwise it is estimated from the levels.
    """
    if ended - started < SHORTEST_CYCLE:
        return None
    if used is None:
        used = max(Decimal(0), full_amount(product) - left_at(level, product))
    cycle = Cycle(started=started, ended=ended, used=used)
    cycles = _load_cycles()
    cycles[product_key] = (cycles.get(product_key, []) + [cycle])[-CYCLES_KEPT:]
    _save_cycles(cycles)
    return cycle


def daily_rate(product_key: str) -> Decimal | None:
    """Average use per day over the kept cycles, or None if none has been measured."""
    cycles = _load_cycles().get(product_key, [])
    days = sum(((cycle.ended - cycle.started).total_seconds() for cycle in cycles), 0.0) / 86400
    if not cycles or days <= 0:
        return None
    return sum((cycle.used for cycle in cycles), Decimal(0)) / Decimal(str(days))


def bookings(product_key: str, start: date, end: date) -> list[Booking]:
    """Booked needs for the product dated from `start` to `end`, both included."""
    if not DEMAND_FILE.exists():
        return []
    raw = yaml.safe_load(DEMAND_FILE.read_text(encoding="utf-8")) or {}
    found = [Booking(**item) for item in (raw.get("demand") or {}).get(product_key) or []]
    return [booking for booking in found if start <= booking.date <= end]


def rate_for(product_key: str, product: Stocked) -> Decimal | None:
    """Measured average use per day, else the typical use from shelf.yaml, else None."""
    measured = daily_rate(product_key)
    return measured if measured is not None else product.typical_daily_use


def stock_now(product: Stocked, level: Level | None, seen_at: datetime | None, rate: Decimal | None,
              now: datetime, count: float | None = None) -> Decimal | None:
    """What is probably on the shelf now: the last reading, less what has been used since. None if never seen.

    A camera count (units it could see) is used as the reading when the product is counted in pieces.
    A shelf still reported full is never estimated below the "low" amount, since the sensor would have said so.
    """
    if level is None or seen_at is None:
        return None
    if count is not None and product.unit == "pcs":
        return Decimal(str(count))
    left = left_at(level, product)
    if rate is None:
        return left
    used = rate * Decimal(str(max(0.0, (now - seen_at).total_seconds()) / 86400))
    floor = left_at("low", product) if level == "full" else Decimal(0)
    return max(floor, left - used) if left > floor else left


def runs_out(product_key: str, product: Stocked, stock: Decimal, today: date) -> date | None:
    """The first day within the next RUNOUT_WINDOW_DAYS (or the lead time, if longer) that usage
    and bookings would take the shelf below zero, or None if it lasts."""
    rate = rate_for(product_key, product) or Decimal(0)
    window = max(RUNOUT_WINDOW_DAYS, _lead(product))
    booked = {}
    for booking in bookings(product_key, today, today + timedelta(days=window)):
        booked[booking.date] = booked.get(booking.date, Decimal(0)) + booking.quantity
    for day in range(window + 1):
        stock -= (rate if day else Decimal(0)) + booked.get(today + timedelta(days=day), Decimal(0))
        if stock < 0:
            return today + timedelta(days=day)
    return None


def _whole(amount: Decimal) -> Decimal:
    return amount.to_integral_value(rounding=ROUND_CEILING)


def reorder(product_key: str, product: Stocked, level: Level | None, today: date,
            on_shelf: Decimal | None = None) -> Forecast:
    """How much to order now, so the shelf lasts until `cover_days` after the delivery arrives."""
    horizon = _lead(product) + product.cover_days
    booked_items = bookings(product_key, today, today + timedelta(days=horizon))
    booked = sum((booking.quantity for booking in booked_items), Decimal(0))
    on_shelf = on_shelf if on_shelf is not None else left_at(level or "low", product)
    measured = daily_rate(product_key)
    rate = rate_for(product_key, product)
    unit = product.unit
    booked_text = (f" plus {booked} {unit} booked ({'; '.join(f'{b.date:%a %d %b}: {b.quantity} {unit}' + (f' {b.note}' if b.note else '') for b in booked_items)})"
                   if booked_items else "")
    if rate is None:
        quantity = _whole((product.reorder_quantity or Decimal(0)) + booked)
        return Forecast(quantity=quantity, daily_rate=None, booked=booked, on_shelf=on_shelf,
                        explanation=f"No usage measured yet, so the usual {product.reorder_quantity} {unit}{booked_text}.")
    needed = rate * horizon + booked - on_shelf
    quantity = max(Decimal(1), _whole(needed))
    return Forecast(quantity=quantity, daily_rate=rate.quantize(Decimal("0.1")), booked=booked, on_shelf=on_shelf,
                    explanation=f"{'Average' if measured is not None else 'Typical (not yet measured)'} use {rate:.1f} {unit}/day over {horizon} days "
                                f"({_lead(product)} until delivery + {product.cover_days} after){booked_text}, "
                                f"minus about {on_shelf:.0f} {unit} still on the shelf.")
