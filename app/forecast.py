"""How much to reorder, and when the shelf will run out.

The order amount is the configured `reorder_quantity` (editable per inventory
item in the dashboard), plus any bookings from config/demand.yaml that fall
inside the cover window.

Usage is still measured per cycle (full after refill → low/empty) so the
agent can forecast run-out within the next week. Without measured cycles,
`typical_daily_use` from shelf.yaml stands in for the rate.

The model clock (model_now / model_days_between) scales real time for demos:
see config/shelf.yaml → reorder_model.model_day_seconds and REORDER_SPEC.md §4.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any, Protocol

import yaml
from pydantic import BaseModel

from app import config, settings
from app.models import Level, Unit

CYCLES_KEPT = 8  # older cycles say little about today's menu
SHORTEST_CYCLE = timedelta(hours=1)  # quicker than this is a restock glitch, not usage
DEFAULT_LOW_FRACTION = Decimal("0.25")  # how full "low" is, when shelf.yaml does not say
RUNOUT_WINDOW_DAYS = 7  # the agent orders before the forecast shows the shelf running out within this many days
USAGE_FILE = settings.DATA_DIR / "usage_cycles.json"
DEMAND_FILE = settings.CONFIG_DIR / "demand.yaml"


def model_day_seconds() -> float:
    """Real seconds that count as one model day (1800 = demo; 86400 = real time)."""
    return float(config.load_reorder_model().model_day_seconds)


def model_now() -> datetime:
    """Current wall-clock time; convert intervals with model_days_between, not raw seconds.

    Calendar dates (Valio delivery, audit timestamps) stay real — only usage rate,
    usage fade, and "delivery due" use the model clock (REORDER_SPEC.md §4).
    """
    from app import state
    return state.now()


def model_days_between(earlier: datetime, later: datetime) -> float:
    """Model days from earlier to later: real_seconds / model_day_seconds."""
    return (later - earlier).total_seconds() / model_day_seconds()


# --- Pure reorder model (REORDER_SPEC.md §§2, 5, 6; README "How Ukko decides…") ---


def tau_from_alpha(alpha: float) -> float:
    """Smoothing time-constant: alpha 0.3 → tau ≈ 2.804 model days (≈ last 3–5 days)."""
    return -1.0 / math.log(1.0 - alpha)


def update_usage_rate(d: float, units: float, dt_model_days: float, alpha: float) -> float:
    """EWMA usage update on a stable drop (REORDER_SPEC.md §5).

    d_new = d * exp(-dt/tau) + units/tau — cold-start fades as observations arrive.
    """
    tau = tau_from_alpha(alpha)
    return d * math.exp(-dt_model_days / tau) + units / tau


def faded_usage_rate(d: float, dt_model_days: float, alpha: float) -> float:
    """Read usage between events: assumed/measured rate fades with the same tau."""
    tau = tau_from_alpha(alpha)
    return d * math.exp(-dt_model_days / tau)


def apply_delivery(on_order_units: float, visible_increase: float, units_per_visible_item: float) -> float:
    """Stock rise = delivery arriving; cut on-order (not below zero). Does not change d."""
    arrived = max(0.0, visible_increase) * units_per_visible_item
    return max(0.0, on_order_units - arrived)


def inventory_position(on_hand_units: float, on_order_units: float) -> float:
    """IP = on hand + already on order (open orders stop a second placement)."""
    return on_hand_units + on_order_units


def get_adjustments(_product_key: str, days: int) -> tuple[list[float], int, list[str]]:
    """Daily demand multipliers and extra lead days from web signals.

    Stub until a real demand-signals module exists: 1.0 every day, no extra lead.
    Returns (daily_multipliers, extra_lead_days, reasons).
    """
    return [1.0] * max(0, int(days)), 0, []


def lead_days_for(
    delivery_date: date | None,
    today: date,
    default_lead_days: int,
    extra_lead_days: int = 0,
) -> int:
    """Days until delivery: from Valio quote when known, else default; plus signal extra."""
    if delivery_date is not None:
        base = max(1, (delivery_date - today).days)
    else:
        base = max(1, int(default_lead_days))
    return base + max(0, int(extra_lead_days))


def reorder_point(
    d: float,
    lead_days: float,
    multipliers: list[float],
    z: float,
    sigma_factor: float,
) -> tuple[float, float, float, float]:
    """Expected lead-time demand, effective daily rate, safety buffer, reorder point.

    D_lead = d * sum(multipliers); B = z * sigma_factor * sqrt(d_eff) * sqrt(lead);
    R = D_lead + B. See README "How Ukko decides when and how much to reorder".
    """
    lead = max(0.0, float(lead_days))
    # One multiplier per day until delivery (pad/truncate to lead_days).
    days = max(1, int(math.ceil(lead))) if lead > 0 else 0
    mults = list(multipliers[:days]) + [1.0] * max(0, days - len(multipliers))
    d_lead = d * sum(mults) if days else 0.0
    d_eff = d_lead / lead if lead > 0 else 0.0
    # Buffer assumes ~Poisson variability scaled by sigma_factor (assumption until measured).
    buffer = (z * sigma_factor * math.sqrt(max(d_eff, 0.0)) * math.sqrt(lead)) if lead > 0 else 0.0
    return d_lead, d_eff, buffer, d_lead + buffer


def should_reorder(ip: float, reorder_pt: float) -> bool:
    """Reorder when inventory position is at or below the reorder point."""
    return ip <= reorder_pt


@dataclass
class OrderSize:
    """Result of sizing an order (REORDER_SPEC.md §6)."""

    S: float
    raw_Q: float
    Q: float
    cap_notes: list[str]


def order_quantity(
    d: float,
    lead_days: float,
    cover_days: int,
    lead_multipliers: list[float],
    cover_multipliers: list[float],
    z: float,
    sigma_factor: float,
    ip: float,
    pack_size: float,
    max_stock_units: float,
    shelf_life_days: int | None = None,
) -> OrderSize:
    """Order-up-to S and pack-rounded Q, with storage and shelf-life caps."""
    d_lead, d_eff, _buffer, reorder_pt = reorder_point(d, lead_days, lead_multipliers, z, sigma_factor)
    cover = max(0, int(cover_days))
    cover_mults = list(cover_multipliers[:cover]) + [1.0] * max(0, cover - len(cover_multipliers))
    # S = R + demand over the cover window after delivery arrives.
    target = reorder_pt + d * sum(cover_mults)
    raw = target - ip
    notes: list[str] = []
    capped = raw
    storage_room = max_stock_units - ip
    if capped > storage_room:
        notes.append(f"storage holds {max_stock_units:g}, so limited to {max(0.0, storage_room):g}")
        capped = storage_room
    if shelf_life_days is not None and d_eff > 0:
        life_cap = d_eff * shelf_life_days
        if capped > life_cap:
            notes.append(f"shelf life {shelf_life_days} days limits the order to {life_cap:g}")
            capped = life_cap
    capped = max(0.0, capped)
    pack = max(1.0, float(pack_size))
    # Round up to whole packs; if that breaks a cap, round down instead.
    rounded_up = math.ceil(capped / pack) * pack if capped > 0 else 0.0
    if rounded_up > 0 and (rounded_up > storage_room or (
            shelf_life_days is not None and d_eff > 0 and rounded_up > d_eff * shelf_life_days)):
        final = math.floor(capped / pack) * pack
    else:
        final = rounded_up
    if final < pack:
        # Below one pack: order one pack if caps allow, else nothing.
        life_ok = shelf_life_days is None or d_eff <= 0 or pack <= d_eff * shelf_life_days
        if pack <= storage_room and life_ok and capped > 0:
            final = pack
        else:
            if capped > 0:
                notes.append("below one pack after caps, so not ordering")
            final = 0.0
    return OrderSize(S=target, raw_Q=raw, Q=final, cap_notes=notes)


def on_hand_units(visible_count: float, units_per_visible_item: float) -> float:
    """Camera-visible items scaled to order units (demo: one can ≈ one case)."""
    return max(0.0, float(visible_count)) * float(units_per_visible_item)


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
    if not isinstance(raw, dict):
        return {}
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
    """How much to order now: usage-based order-up-to level (REORDER_SPEC.md §6).

    Falls back to configured reorder_quantity only when the model yields nothing
    and a configured amount exists (legacy shelf rows without camera counts).
    """
    model = model_order_for(product_key, product, today=today, on_shelf=on_shelf, level=level)
    if model is not None:
        return model
    # Legacy path: fixed reorder_quantity + bookings (typed/non-camera products).
    horizon = _lead(product) + product.cover_days
    booked_items = bookings(product_key, today, today + timedelta(days=horizon))
    booked = sum((booking.quantity for booking in booked_items), Decimal(0))
    on_shelf = on_shelf if on_shelf is not None else left_at(level or "low", product)
    measured = daily_rate(product_key)
    rate = rate_for(product_key, product)
    unit = product.unit
    configured = product.reorder_quantity or Decimal(0)
    quantity = _whole(configured + booked)
    booked_text = (f" plus {booked} {unit} booked ({'; '.join(f'{b.date:%a %d %b}: {b.quantity} {unit}' + (f' {b.note}' if b.note else '') for b in booked_items)})"
                   if booked_items else "")
    rate_text = ""
    daily = None
    if rate is not None:
        daily = rate.quantize(Decimal("0.1"))
        label = "Average" if measured is not None else "Typical (not yet measured)"
        rate_text = f" {label} use {rate:.1f} {unit}/day; about {on_shelf:.0f} {unit} still on the shelf."
    return Forecast(
        quantity=max(Decimal(1), quantity),
        daily_rate=daily, booked=booked, on_shelf=on_shelf,
        explanation=f"Configured order of {configured} {unit}{booked_text}.{rate_text}".strip(),
    )


def _row_usage_d(product: Any, now: datetime | None = None) -> tuple[float, int]:
    """Current faded usage rate and observation count for a shelf row or Stocked product."""
    model = config.load_reorder_model()
    typical = float(product.typical_daily_use or 8)
    observations = int(getattr(product, "usage_observations", 0) or 0)
    stored = getattr(product, "usage_rate_d", None)
    updated = getattr(product, "usage_updated_at", None)
    d = float(stored) if stored is not None else typical
    if updated is not None and now is not None:
        d = faded_usage_rate(d, max(0.0, model_days_between(updated, now)), model.alpha)
    return d, observations


def evaluate_row(product_key: str, product: Any, *, now: datetime | None = None,
                 visible: float | None = None) -> dict[str, Any]:
    """Live reorder-model numbers for one shelf row (dashboard + trigger)."""
    model = config.load_reorder_model()
    now = now or model_now()
    today = now.date()
    units_per = float(getattr(product, "units_per_visible_item", None) or 1)
    pack = float(getattr(product, "pack_size", None) or 24)
    max_stock = float(getattr(product, "max_stock_units", None) or 144)
    cover = int(getattr(product, "cover_days", None) or model.default_cover_days)
    service = str(getattr(product, "service_class", None) or "standard")
    shelf_life = getattr(product, "shelf_life_days", None)
    on_order = float(getattr(product, "on_order_units", 0) or 0)
    visible_count = float(visible if visible is not None else (getattr(product, "quantity", None) or 0))
    d, observations = _row_usage_d(product, now)
    default_lead = int(getattr(product, "lead_time_days", None) or model.default_lead_days)
    lead_base = default_lead
    mults_lead, extra, reasons = get_adjustments(product_key, lead_base)
    lead = lead_days_for(None, today, default_lead, extra)
    if lead != lead_base:
        mults_lead, extra, reasons = get_adjustments(product_key, lead)
        lead = lead_days_for(None, today, default_lead, extra)
    z = model.z_for(service)
    on_hand = on_hand_units(visible_count, units_per)
    ip = inventory_position(on_hand, on_order)
    d_lead, d_eff, buf, reorder_pt = reorder_point(d, lead, mults_lead, z, model.sigma_factor)
    cover_mults, _, _ = get_adjustments(product_key, cover)
    size = order_quantity(
        d, lead, cover, mults_lead, cover_mults, z, model.sigma_factor,
        ip, pack, max_stock, shelf_life_days=shelf_life,
    )
    today_mult = (mults_lead[0] if mults_lead else 1.0)
    daily_use = d * today_mult
    days_cover = (ip / daily_use) if daily_use > 0.05 else None
    delivery_due = False
    since = getattr(product, "on_order_since", None)
    lead_for_due = getattr(product, "on_order_lead_days", None)
    if on_order > 0 and since is not None and lead_for_due is not None:
        delivery_due = model_days_between(since, now) >= float(lead_for_due)
    service_pct = {"critical": "99%", "standard": "95%", "perishable": "90%"}.get(service, "95%")
    rate_label = (
        f"measured from {observations} observations" if observations > 0 else "assumed"
    )
    needs = should_reorder(ip, reorder_pt)
    why = (
        f"Usage is {d:.1f} a day ({rate_label}). Delivery takes {lead:g} days, so we expect to use "
        f"{d_lead:.1f} before it arrives. Safety buffer {buf:.1f} ({service_pct} service level). "
        f"Reorder point {reorder_pt:.1f}. Stock {on_hand:.0f} + on order {on_order:.0f} = {ip:.0f}, "
        f"which is {'at or below' if needs else 'above'} {reorder_pt:.1f}, so "
        f"{'reordering' if needs else 'not reordering'}."
    )
    qty_why = (
        f"Lasting {lead:g} days until delivery and {cover} days after needs {size.S:.1f}. "
        f"Minus {ip:.0f} in stock/on order, that is {size.raw_Q:.1f}."
    )
    if size.cap_notes:
        qty_why += " " + "; ".join(size.cap_notes).capitalize() + "."
    if size.Q > 0:
        packs = int(round(size.Q / pack)) if pack else 0
        qty_why += f" Order {size.Q:.0f} ({packs} pack(s) of {pack:g})."
    else:
        qty_why += " No order."
    if reasons:
        why += " Web signals: " + "; ".join(reasons) + "."
    return {
        "usage_per_day": round(d, 2),
        "usage_label": rate_label,
        "usage_observations": observations,
        "days_of_cover": None if days_cover is None else round(days_cover, 1),
        "reorder_point": round(reorder_pt, 1),
        "on_hand_units": round(on_hand, 1),
        "on_order_units": round(on_order, 1),
        "inventory_position": round(ip, 1),
        "needs_reorder": needs,
        "order_quantity": size.Q,
        "lead_days": lead,
        "delivery_due": delivery_due,
        "units_per_visible_item": units_per,
        "why": why,
        "quantity_why": qty_why,
        "model_day_seconds": model.model_day_seconds,
        "d_lead": d_lead,
        "buffer": buf,
        "service_class": service,
        "z": z,
    }


def uses_reorder_model(product: Any) -> bool:
    """Piece-counted camera products use the usage-based model; litres/kg keep the legacy path."""
    return getattr(product, "unit", None) == "pcs"


def model_order_for(
    product_key: str,
    product: Stocked,
    *,
    today: date | None = None,
    on_shelf: Decimal | None = None,
    level: Level | None = None,
) -> Forecast | None:
    """Build a Forecast from the reorder model for camera piece products."""
    if not uses_reorder_model(product):
        return None
    units_per = getattr(product, "units_per_visible_item", None)
    if units_per is None and getattr(product, "pack_size", None) is None:
        return None
    now = model_now()
    today = today or now.date()
    visible = None
    if on_shelf is not None and units_per:
        # on_shelf may already be visible count (pcs) or units — prefer row.quantity when present.
        visible = float(getattr(product, "quantity", None) or 0)
    view = evaluate_row(product_key, product, now=now, visible=visible)
    qty = Decimal(str(int(view["order_quantity"]))) if view["order_quantity"] else Decimal(0)
    if qty <= 0:
        configured = getattr(product, "reorder_quantity", None) or Decimal(0)
        if configured > 0:
            qty = _whole(configured)
            explanation = view["quantity_why"] + f" Falling back to configured {configured}."
        else:
            return Forecast(
                quantity=Decimal(0), daily_rate=Decimal(str(round(view["usage_per_day"], 1))),
                booked=Decimal(0), on_shelf=Decimal(str(view["on_hand_units"])),
                explanation=view["why"] + " " + view["quantity_why"],
            )
    else:
        explanation = view["why"] + " " + view["quantity_why"]
    # Bookings still add on top when present.
    horizon = int(view["lead_days"]) + int(getattr(product, "cover_days", 7) or 7)
    booked_items = bookings(product_key, today, today + timedelta(days=horizon))
    booked = sum((booking.quantity for booking in booked_items), Decimal(0))
    if booked:
        qty = _whole(qty + booked)
        explanation += f" Plus {booked} booked."
    return Forecast(
        quantity=max(Decimal(1), qty) if qty > 0 else Decimal(0),
        daily_rate=Decimal(str(round(view["usage_per_day"], 1))),
        booked=booked,
        on_shelf=Decimal(str(view["on_hand_units"])),
        explanation=explanation.strip(),
    )


def seed_usage(product: Any, when: datetime | None = None) -> None:
    """Cold-start the EWMA from typical_daily_use if not yet set."""
    if getattr(product, "usage_rate_d", None) is None:
        product.usage_rate_d = float(product.typical_daily_use or 8)
        product.usage_updated_at = when or model_now()
