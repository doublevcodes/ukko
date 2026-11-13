"""Read and write the files in config/: policy rules and shelf products."""

from __future__ import annotations

from decimal import Decimal

import yaml
from pydantic import BaseModel

from app import settings
from app.models import PolicyConfig, Unit

POLICY_HEADER = (
    "# Spending and approval rules. Saved from the dashboard.\n"
    "# auto_approve_max_total_eur: orders above this total (EUR, excluding VAT) need a person.\n"
    "# max_quantity_multiplier_vs_usual: more than this many times the usual amount needs a person.\n"
    "# allowed_categories: product groups the agent may buy on its own (names from categories.yaml).\n"
    "# agent_budget_eur: the most the agent may spend in total on its own; the supplier enforces it.\n"
)


class ShelfProduct(BaseModel):
    """One watched product from config/shelf.yaml."""

    name: str
    product_id: str
    reorder_quantity: Decimal
    unit: Unit
    usual_quantity: Decimal | None = None
    constraints: list[str] = []
    preferences: list[str] = []
    lead_time_days: int = 2
    search_terms: str = ""


def load_policy() -> PolicyConfig:
    """Read config/policy.yaml. A missing file means the built-in defaults."""
    if not settings.POLICY_FILE.exists():
        return PolicyConfig()
    return PolicyConfig(**(yaml.safe_load(settings.POLICY_FILE.read_text(encoding="utf-8")) or {}))


def save_policy(policy: PolicyConfig) -> None:
    def plain(value: object) -> object:
        """Write 500 as 500 and 2.5 as 2.5, so the file stays readable."""
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        return value

    values = {key: plain(value) for key, value in policy.model_dump().items()}
    text = POLICY_HEADER + yaml.safe_dump(values, sort_keys=False, allow_unicode=True)
    settings.POLICY_FILE.write_text(text, encoding="utf-8")


def load_shelf() -> dict[str, ShelfProduct]:
    if not settings.SHELF_FILE.exists():
        return {}
    raw = yaml.safe_load(settings.SHELF_FILE.read_text(encoding="utf-8")) or {}
    return {key: ShelfProduct(**value) for key, value in (raw.get("products") or {}).items()}
