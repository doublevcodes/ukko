"""Live state in memory, plus the append-only audit log on disk.

Restarting Stockpot clears the live state. The audit file is kept.
"""

from __future__ import annotations

import secrets
import threading
from collections import deque
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

from app import config, settings
from app.models import AuditEntry, Decision, Level, Offer, OrderRequest, PolicyConfig, Unit

OPEN_STATUSES = {"working", "needs_clarification", "awaiting_approval"}
AUDIT_SHOWN = 100
REQUESTS_SHOWN = 20


class RequestRecord(BaseModel):
    """Everything known about one request while Stockpot is running."""

    id: str
    created_at: datetime
    source: Literal["sensor", "typed"]
    title: str
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
    cover_days: int = 7
    typical_daily_use: Decimal | None = None


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


def load() -> None:
    """Read the config files and start with an empty list of requests.

    Shelf rows are seeded from the camera encounter list (config/camera_foods.yaml),
    with reorder fields filled from that file plus shelf.yaml defaults.
    """
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
                low_quantity=fields["low_quantity"], cover_days=fields["cover_days"],
                typical_daily_use=fields["typical_daily_use"],
            )


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
    return entry


def new_request(source: Literal["sensor", "typed"], title: str, product_key: str | None = None) -> RequestRecord:
    request_id = f"req-{now():%m%d-%H%M%S}-{secrets.token_hex(2)}"
    record = RequestRecord(id=request_id, created_at=now(), source=source, title=title, product_key=product_key,
                           decision=Decision(request_id=request_id))
    with lock:
        requests[request_id] = record
    return record


def open_request_for(product_key: str) -> RequestRecord | None:
    with lock:
        for record in requests.values():
            if record.product_key == product_key and record.decision.status in OPEN_STATUSES:
                return record
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
    """One request as the dashboard and the phone page see it."""
    view = record.model_dump(mode="json", exclude={"offers", "candidate_ids", "approval_token", "timeline"})
    view["timeline"] = [entry.model_dump(mode="json", exclude={"details"}) for entry in record.timeline]
    return without_signatures(view)


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
