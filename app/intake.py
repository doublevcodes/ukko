"""Where work enters the pipeline: typed requests, sensor events, approvals.

Each `run_*` function is started as a background task, so the HTTP call
returns at once and the dashboard shows progress as it polls.
"""

from __future__ import annotations

import threading
from typing import Callable

from app import config, forecast, pipeline, state, vision
from app.models import SensorEvent
from app.state import RequestRecord


def _guarded(record: RequestRecord, work: Callable[[], None]) -> None:
    """Run pipeline work so that a crash can never leave a request stuck on "working"."""
    try:
        work()
    except Exception as error:  # noqa: BLE001  last line of defence; the details go to the log
        problem = f"Something unexpected went wrong ({type(error).__name__}: {str(error)[:200]})."
        if record.decision.order_confirmation is None:
            pipeline.fail(record, "error", problem)
        else:
            # The order exists at the supplier; do not report it as "nothing was ordered".
            state.log(record.id, "error", f"{problem} The order had already been placed.")
    pipeline.finalise(record)


def new_typed_request(text: str, reply_to: str | None = None) -> RequestRecord:
    """Create the record for a typed request. An answer to a question is joined to the original text."""
    earlier = state.requests.get(reply_to) if reply_to else None
    if earlier and earlier.decision.status == "needs_clarification" and earlier.source == "typed":
        text = f"{earlier.title}\n(Stockpot asked: {earlier.decision.message})\nAnswer: {text}"
        with state.lock:
            # The question is answered, so the half-finished request leaves the dashboard.
            # Its entries stay in the audit log.
            state.requests.pop(earlier.id, None)
    record = state.new_request("typed", text)
    state.log(record.id, "request", f"Someone typed: \"{text}\"")
    return record


def run_typed(record: RequestRecord) -> None:
    def work() -> None:
        if pipeline.parse_request(record, record.title) and pipeline.lookup(record):
            pipeline.decide(record)
    _guarded(record, work)


def sensor_event(event: SensorEvent) -> tuple[str, RequestRecord | None]:
    """Record a shelf level. Returns what happened, and a new request if one was raised.

    One request per run-low: a repeated "low" or "empty" is ignored while a
    request for that product is open, and afterwards until the shelf has been
    seen full again.
    """
    with state.lock:
        level = state.shelf.get(event.product_key)
        if level is None:
            state.log(None, "sensor", f"Ignored a sensor event for an unknown product key '{event.product_key}'.")
            return "unknown product", None
        was = level.level
        level.level, level.confidence = event.level, event.confidence
        level.updated_at, level.device_id = state.now(), event.device_id
        if event.level == "full":
            level.armed = True
            if was != "full":
                level.full_since = level.updated_at
            return "level recorded", None
        cycle_started, level.full_since = level.full_since, None
        open_request = state.open_request_for(event.product_key)
        if open_request or not level.armed:
            why = "a request for it is already open" if open_request else "it was already handled and the shelf has not been refilled"
            state.log(open_request.id if open_request else None, "sensor",
                      f"Ignored duplicate: {level.name} reported '{event.level}' again, but {why}.")
            return "ignored duplicate", None
        level.armed = False
        record = state.new_request("sensor", f"Shelf sensor: {level.name} is {event.level}", event.product_key)
    cycle = cycle_started and forecast.record_cycle(event.product_key, level, cycle_started, level.updated_at, event.level)
    if cycle:
        hours = (cycle.ended - cycle.started).total_seconds() / 3600
        state.log(record.id, "sensor", f"Usage measured: about {cycle.used:.0f} {level.unit} of {level.name} in {hours:.1f} hours.")
    state.log(record.id, "sensor", f"The shelf sensor ({event.device_id}) reports {level.name} is {event.level} "
              f"(confidence {event.confidence:.0%}). Starting a reorder.")
    return "request created", record


def _reorder_from_shelf_row(record: RequestRecord) -> bool:
    """Build the order request from a camera/sensor shelf row (exact id or catalogue search)."""
    key = record.product_key
    if not key:
        pipeline.fail(record, "parse", "The reorder had no product key.")
        return False
    configured = config.load_shelf().get(key)
    with state.lock:
        row = state.shelf.get(key)
    if configured is not None:
        return pipeline.request_from_shelf(record, configured)
    if row is not None:
        return pipeline.request_from_vision(record, row)
    pipeline.fail(record, "parse", f"No shelf row left for '{key}'.")
    return False


def check_forecasts() -> list[RequestRecord]:
    """Raise a reorder for every product the forecast shows running out within the next 7 days.

    Same rule as the sensor: only products seen full since their last reorder, and never
    while a request for the product is open.
    """
    raised = []
    with state.lock:
        keys = list(state.shelf)
    for key in keys:
        with state.lock:
            level = state.shelf.get(key)
            # Only products seen full since their last reorder: a food the camera has never seen
            # stocked (not in frame) must not order, and a delivery on its way is not ordered twice.
            if level is None or level.full_since is None or not level.armed or state.open_request_for(key):
                continue
            stock = forecast.stock_now(level, level.level, level.updated_at, forecast.rate_for(key, level),
                                       state.now(), level.quantity)
            if stock is None:
                continue
            empty_on = forecast.runs_out(key, level, stock, state.now().date())
            if empty_on is None:
                continue
            level.armed = False
            record = state.new_request("sensor", f"Forecast: {level.name} runs out by {empty_on:%a %d %b}", key)
        state.log(record.id, "forecast", f"About {stock:.0f} {level.unit} of {level.name} is left; at the expected use and "
                  f"bookings it runs out by {empty_on:%a %d %b}, within {forecast.RUNOUT_WINDOW_DAYS} days. Starting a reorder.")
        raised.append(record)
    return raised


def run_sensor(record: RequestRecord) -> None:
    def work() -> None:
        if _reorder_from_shelf_row(record) and pipeline.lookup(record):
            pipeline.decide(record)
    _guarded(record, work)


def run_vision_reorder(record: RequestRecord) -> None:
    """Reorder from a vision low/empty reading: exact Valio id when known, else closest catalogue match."""
    def work() -> None:
        if _reorder_from_shelf_row(record) and pipeline.lookup(record):
            pipeline.decide(record)
    _guarded(record, work)


def _start_vision_reorders(records: list[RequestRecord]) -> None:
    for record in records:
        threading.Thread(target=run_vision_reorder, args=(record,), name=f"vision-reorder-{record.id}",
                         daemon=True).start()


def wire_vision_reorders() -> None:
    """Let the vision loop start reorders without going through HTTP."""
    vision.set_reorder_handler(_start_vision_reorders)


def claim_approval(record: RequestRecord) -> bool:
    """Take the pending approval so that two clicks cannot both act on it."""
    with state.lock:
        if record.decision.status != "awaiting_approval":
            return False
        record.decision.status, record.approval_token = "working", None
        return True


def run_approved(record: RequestRecord, note: str, where: str) -> None:
    """A person approved: their key signs this exact order, after a fresh re-check."""
    said = f" Note: {note}" if note else ""
    state.log(record.id, "approval", f"A person approved the plan from the {where}.{said}")
    _guarded(record, lambda: pipeline.act(record, approved_by_person=True))


def reject(record: RequestRecord, note: str, where: str) -> None:
    said = f" Note: {note}" if note else ""
    record.decision.status, record.decision.message = "rejected", f"A person rejected the plan. {pipeline.NOTHING_ORDERED}"
    state.log(record.id, "approval", f"A person rejected the plan from the {where}.{said} {pipeline.NOTHING_ORDERED}")
    pipeline.finalise(record)
