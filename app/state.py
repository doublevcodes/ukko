"""Live state in memory, plus the append-only audit log on disk.

On a normal laptop restart, requests are cleared and the audit file is kept.
On Vercel, live_state.json (and the audit file) are also mirrored to Blob so a
cold start after a camera blip does not wipe the kitchen card or order history.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections import deque
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

from app import config, durable, settings
from app.models import AuditEntry, Decision, Level, Offer, OrderRequest, PolicyConfig, Unit

OPEN_STATUSES = {"working", "needs_clarification", "awaiting_approval"}
# Camera must not start another order while one of these is the latest for that product
# (ordered stays blocked until the shelf is confirmed full and re-armed).
REORDER_BLOCK_STATUSES = OPEN_STATUSES | {"ordered"}
AUDIT_SHOWN = 100
REQUESTS_SHOWN = 20


class RequestRecord(BaseModel):
    """Everything known about one request while Ukko is running."""

    id: str
    created_at: datetime
    source: Literal["sensor", "typed"]
    title: str
    # Full typed text including clarification Q&A for the parser; title stays the original ask.
    parse_text: str | None = None
    product_key: str | None = None
    request: OrderRequest | None = None
    decision: Decision
    timeline: list[AuditEntry] = []
    offers: list[Offer] = []  # live offers for the candidates; not sent to the dashboard
    candidate_ids: list[str] = []
    exact_id: str | None = None
    approval_token: str | None = None  # one-time code in the phone approval link
    replans: int = 0


class ShelfLevel(BaseModel):
    product_key: str
    name: str
    level: Level | None = None
    confidence: float | None = None
    updated_at: datetime | None = None
    device_id: str | None = None
    # How many units the laptop vision model can see right now (app/vision.py). None before the first look.
    quantity: float | None = None
    # False after a request was raised for this product; True again once the shelf is seen full.
    armed: bool = True
    # Consecutive low/empty vision looks after a full; reorder waits until this reaches CONFIRM_LOOKS.
    low_streak: int = 0
    # Recent raw counts from vision; quantity/level only update when the last few agree.
    recent_counts: list[float] = []
    # When the shelf was first seen full after a refill, and how many units were visible then:
    # the start of the usage cycle (app/forecast.py).
    full_since: datetime | None = None
    full_count: float | None = None
    # Reorder settings from camera_foods.yaml + shelf.yaml defaults.
    product_id: str | None = None
    reorder_quantity: Decimal | None = None
    unit: Unit | None = None
    usual_quantity: Decimal | None = None
    constraints: list[str] = []
    preferences: list[str] = []
    lead_time_days: int | None = None
    search_terms: str = ""
    full_quantity: Decimal | None = None
    low_quantity: Decimal | None = None
    # Order when the camera sees this many units or fewer (default 2 from shelf.yaml).
    reorder_below: Decimal | None = None
    cover_days: int = 7
    typical_daily_use: Decimal | None = None
    # Reorder model (REORDER_SPEC.md): service class, case size, packs, storage, shelf life.
    service_class: str = "standard"
    units_per_visible_item: Decimal = Decimal(1)
    pack_size: Decimal = Decimal(24)
    max_stock_units: Decimal = Decimal(144)
    shelf_life_days: int | None = None
    # Live usage estimate (units per model day) and open-order tracking.
    usage_rate_d: float | None = None
    usage_updated_at: datetime | None = None
    usage_observations: int = 0
    on_order_units: float = 0.0
    on_order_since: datetime | None = None
    on_order_lead_days: float | None = None
    # Plain-language "why" for the last model decision (dashboard expander).
    reorder_why: str | None = None


class Box(BaseModel):
    """A rectangle in the camera picture, as fractions (0 to 1) of the picture's width and height."""

    x: float
    y: float
    w: float
    h: float


class Detection(BaseModel):
    """Where the vision model saw a watched product in the camera picture (app/vision.py)."""

    product_key: str
    name: str
    box: Box
    quantity: float = 1  # estimated units inside the box (may be fractional when the model is unsure)
    confidence: float
    updated_at: datetime


lock = threading.RLock()
requests: dict[str, RequestRecord] = {}
shelf: dict[str, ShelfLevel] = {}
detections: list[Detection] = []
audit: deque[AuditEntry] = deque(maxlen=AUDIT_SHOWN)
policy: PolicyConfig = PolicyConfig()


def now() -> datetime:
    return datetime.now(settings.HELSINKI).replace(microsecond=0)


def _live_state_path():
    return settings.DATA_DIR / "live_state.json"


def save() -> None:
    """Write requests, shelf and recent audit to disk (and Blob on Vercel)."""
    with lock:
        payload = {
            "version": 1,
            "policy": policy.model_dump(mode="json"),
            "requests": [record.model_dump(mode="json") for record in requests.values()],
            "shelf": {key: level.model_dump(mode="json") for key, level in shelf.items()},
            "audit": [entry.model_dump(mode="json") for entry in audit],
        }
    path = _live_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    durable.mirror(path)
    if settings.AUDIT_LOG.exists():
        durable.mirror(settings.AUDIT_LOG)


def _restore_live_state() -> None:
    """Reload requests/shelf/audit from live_state.json when present (after a cold start)."""
    global policy
    path = _live_state_path()
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    with lock:
        if payload.get("policy"):
            try:
                policy = PolicyConfig(**payload["policy"])
            except Exception:  # noqa: BLE001
                pass
        requests.clear()
        for raw in payload.get("requests") or []:
            try:
                record = RequestRecord(**raw)
                requests[record.id] = record
            except Exception:  # noqa: BLE001
                continue
        for key, raw in (payload.get("shelf") or {}).items():
            if key not in shelf:
                continue
            try:
                restored = ShelfLevel(**raw)
            except Exception:  # noqa: BLE001
                continue
            # Keep yaml-driven catalogue fields; restore the live sensor/order bits.
            current = shelf[key]
            current.level = restored.level
            current.confidence = restored.confidence
            current.updated_at = restored.updated_at
            current.device_id = restored.device_id
            current.quantity = restored.quantity
            current.armed = restored.armed
            current.low_streak = restored.low_streak
            current.recent_counts = restored.recent_counts
            current.full_since = restored.full_since
            current.full_count = restored.full_count
            current.usage_rate_d = restored.usage_rate_d
            current.usage_updated_at = restored.usage_updated_at
            current.usage_observations = restored.usage_observations
            current.on_order_units = restored.on_order_units
            current.on_order_since = restored.on_order_since
            current.on_order_lead_days = restored.on_order_lead_days
            current.reorder_why = restored.reorder_why
            current.reorder_below = restored.reorder_below or current.reorder_below
            current.reorder_quantity = restored.reorder_quantity or current.reorder_quantity
        audit.clear()
        for raw in payload.get("audit") or []:
            try:
                audit.append(AuditEntry(**raw))
            except Exception:  # noqa: BLE001
                continue


def load() -> None:
    """Read the config files and start with an empty list of requests.

    Shelf rows are seeded from the camera encounter list (config/camera_foods.yaml),
    with reorder fields filled from that file plus shelf.yaml defaults. If a
    previous run left live_state.json (or Blob restored it), that snapshot wins.
    """
    _seed_from_config()
    _restore_live_state()


def _seed_from_config() -> None:
    """Fresh shelf/requests/audit from yaml — no live_state overlay."""
    global policy
    defaults = config.load_shelf_defaults()
    with lock:
        policy = config.load_policy()
        requests.clear()
        audit.clear()
        detections.clear()
        shelf.clear()
        for key, food in config.load_camera_foods().items():
            fields = config.resolved_reorder(food, defaults)
            shelf[key] = ShelfLevel(
                product_key=key, name=food.name, product_id=food.product_id,
                reorder_quantity=fields["reorder_quantity"], unit=fields["unit"],
                usual_quantity=fields["usual_quantity"], constraints=fields["constraints"],
                preferences=fields["preferences"], lead_time_days=fields["lead_time_days"],
                search_terms=fields["search_terms"], full_quantity=fields["full_quantity"],
                low_quantity=fields["low_quantity"], reorder_below=fields["reorder_below"],
                cover_days=fields["cover_days"], typical_daily_use=fields["typical_daily_use"],
                service_class=str(fields["service_class"]),
                units_per_visible_item=fields["units_per_visible_item"],  # type: ignore[arg-type]
                pack_size=fields["pack_size"],  # type: ignore[arg-type]
                max_stock_units=fields["max_stock_units"],  # type: ignore[arg-type]
                shelf_life_days=fields["shelf_life_days"],  # type: ignore[arg-type]
            )


def reset_runtime() -> None:
    """Wipe requests, shelf live fields, audit and usage history; keep card and keys."""
    from app import forecast

    _seed_from_config()
    settings.AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    settings.AUDIT_LOG.write_text("", encoding="utf-8")
    forecast.USAGE_FILE.write_text("{}\n", encoding="utf-8")
    orders = settings.DATA_DIR / "gateway" / "orders.jsonl"
    if orders.parent.exists():
        orders.write_text("", encoding="utf-8")
    supplier = settings.DATA_DIR / "gateway" / "supplier_log.jsonl"
    if supplier.parent.exists():
        supplier.write_text("", encoding="utf-8")
    try:
        from gateway import merchant, supplier_log

        merchant.reset()
        supplier_log.reset()
    except Exception:  # noqa: BLE001  gateway may not be imported in every process
        pass
    save()
    durable.mirror(forecast.USAGE_FILE)
    durable.mirror(orders)
    if supplier.parent.exists():
        durable.mirror(supplier)
    log(None, "system", "Runtime state was reset: requests, shelf, audit and on-order cleared.")


def set_detections(found: list[Detection]) -> None:
    """Replace the product boxes the dashboard draws over the camera picture."""
    with lock:
        detections[:] = found


def log(request_id: str | None, step: str, summary: str, **details: Any) -> AuditEntry:
    """Write one plain-language entry to the audit file, the dashboard and the request's timeline."""
    entry = AuditEntry(timestamp=now(), request_id=request_id, step=step, summary=summary, details=details)
    with lock:
        audit.append(entry)
        if request_id in requests:
            requests[request_id].timeline.append(entry)
        settings.AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with settings.AUDIT_LOG.open("a", encoding="utf-8") as file:
            file.write(entry.model_dump_json() + "\n")
    save()
    return entry


def new_request(source: Literal["sensor", "typed"], title: str, product_key: str | None = None) -> RequestRecord:
    request_id = f"req-{now():%m%d-%H%M%S}-{secrets.token_hex(2)}"
    record = RequestRecord(id=request_id, created_at=now(), source=source, title=title, product_key=product_key,
                           decision=Decision(request_id=request_id))
    with lock:
        requests[request_id] = record
    save()
    return record


def open_request_for(product_key: str) -> RequestRecord | None:
    with lock:
        for record in requests.values():
            if record.product_key == product_key and record.decision.status in OPEN_STATUSES:
                return record
    return None


def latest_request_for(product_key: str) -> RequestRecord | None:
    """The newest request for this shelf product, if any."""
    latest = None
    with lock:
        for record in requests.values():
            if record.product_key == product_key:
                latest = record
    return latest


def blocks_camera_reorder(product_key: str) -> RequestRecord | None:
    """A request that should stop the camera starting another order for this product.

    In-progress work always blocks. An already-placed order blocks until the shelf
    has been confirmed full again (armed), so a low reading cannot double-order.
    """
    latest = latest_request_for(product_key)
    if latest is None:
        return None
    status = latest.decision.status
    if status in OPEN_STATUSES:
        return latest
    if status == "ordered":
        with lock:
            row = shelf.get(product_key)
        if row is not None and not row.armed:
            return latest
    return None


def by_token(token: str) -> RequestRecord | None:
    with lock:
        return next((record for record in requests.values() if token and record.approval_token == token), None)


def without_signatures(value: Any) -> Any:
    """Drop the long signed offer tokens: the dashboard does not need them every two seconds."""
    if isinstance(value, dict):
        return {key: without_signatures(item) for key, item in value.items() if key != "offer_jwt"}
    if isinstance(value, list):
        return [without_signatures(item) for item in value]
    return value


def request_view(record: RequestRecord) -> dict[str, Any]:
    """One request as the dashboard and the phone page see it.

    Keeps offer_jwt on the chosen plan so the Verification panel can show the
    signed token. Audit finalise still strips tokens via without_signatures.
    """
    view = record.model_dump(
        mode="json",
        exclude={"offers", "candidate_ids", "approval_token", "timeline", "parse_text"},
    )
    view["timeline"] = [entry.model_dump(mode="json", exclude={"details"}) for entry in record.timeline]
    return view


def snapshot() -> dict[str, Any]:
    """The parts of GET /state that live here (main.py adds approval links and AP2 status)."""
    with lock:
        newest = sorted(requests.values(), key=lambda record: record.created_at, reverse=True)[:REQUESTS_SHOWN]
        return {
            "now": now().isoformat(),
            "shelf": [level.model_dump(mode="json") for level in shelf.values()],
            "detections": [found.model_dump(mode="json") for found in detections],
            "requests": [request_view(record) for record in newest],
            "pending_approvals": [r.id for r in newest if r.decision.status == "awaiting_approval"],
            "audit": [entry.model_dump(mode="json", exclude={"details"}) for entry in reversed(audit)],
            "policy": policy.model_dump(mode="json"),
        }
