"""Short ways to drive the pipeline in tests, without HTTP or background tasks."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from app import intake, state
from app.models import PolicyConfig, SensorEvent
from app.state import RequestRecord


def event(level: str = "low", product_key: str = "milk_lactose_free") -> SensorEvent:
    return SensorEvent(product_key=product_key, level=level, confidence=0.9,
                       timestamp=datetime.now(timezone.utc), device_id="test-device")


def shelf_runs_low() -> RequestRecord:
    """A "low" sensor event, followed by the whole pipeline run."""
    result, record = intake.sensor_event(event())
    assert result == "request created"
    intake.run_sensor(record)
    return record


def typed(world: Any, text: str, **answer: Any) -> RequestRecord:
    """A typed request, with the language model's reading of it supplied by the test."""
    reading = {"is_purchase_request": True, "item_description": "", "search_terms": "", "constraints": [],
               "preferences": [], "unknown_hard_requirements": [], "unknown_preferences": [], "clarifying_question": ""}
    world.llm.parse_answer = reading | answer
    record = intake.new_typed_request(text)
    intake.run_typed(record)
    return record


def approve(record: RequestRecord) -> None:
    assert intake.claim_approval(record)
    intake.run_approved(record, "", "test")


def in_days(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def log_text(record: RequestRecord) -> str:
    return " ".join(entry.summary for entry in record.timeline)


def set_limit(eur: int) -> None:
    state.policy = PolicyConfig(**(state.policy.model_dump() | {"auto_approve_max_total_eur": eur}))
