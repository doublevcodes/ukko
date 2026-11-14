"""Read and write the files in config/: policy rules, shelf defaults, and camera foods."""

from __future__ import annotations

from decimal import Decimal

import yaml
from pydantic import BaseModel, field_validator

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
    """One watched product from config/shelf.yaml (optional overrides; mostly unused now)."""

    name: str
    product_id: str
    reorder_quantity: Decimal
    unit: Unit
    usual_quantity: Decimal | None = None
    constraints: list[str] = []
    preferences: list[str] = []
    lead_time_days: int = 2
    search_terms: str = ""


class ShelfDefaults(BaseModel):
    """Reorder settings for camera-recognised foods that do not override them."""

    reorder_quantity: Decimal = Decimal(24)
    unit: Unit = "pcs"
    usual_quantity: Decimal | None = Decimal(24)
    lead_time_days: int = 2
    search_terms: str = ""
    constraints: list[str] = []
    preferences: list[str] = []


class CameraFood(BaseModel):
    """One food the shelf camera may see (config/camera_foods.yaml)."""

    name: str
    product_id: str | None = None
    search_terms: str = ""
    constraints: list[str] = []
    preferences: list[str] = []
    reorder_quantity: Decimal | None = None
    unit: Unit | None = None
    usual_quantity: Decimal | None = None
    lead_time_days: int | None = None

    @field_validator("product_id", mode="before")
    @classmethod
    def _blank_product_id(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value


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


def _shelf_file() -> dict:
    if not settings.SHELF_FILE.exists():
        return {}
    return yaml.safe_load(settings.SHELF_FILE.read_text(encoding="utf-8")) or {}


def load_shelf() -> dict[str, ShelfProduct]:
    raw = _shelf_file()
    return {key: ShelfProduct(**value) for key, value in (raw.get("products") or {}).items()}


def load_shelf_defaults() -> ShelfDefaults:
    """Defaults for camera-recognised shelf rows."""
    return ShelfDefaults(**(_shelf_file().get("defaults") or {}))


def load_camera_foods() -> dict[str, CameraFood]:
    """Foods the camera may label. Missing file means an empty encounter list."""
    if not settings.CAMERA_FOODS_FILE.exists():
        return {}
    raw = yaml.safe_load(settings.CAMERA_FOODS_FILE.read_text(encoding="utf-8")) or {}
    return {key: CameraFood(**value) for key, value in (raw.get("foods") or {}).items()}


def resolved_reorder(food: CameraFood, defaults: ShelfDefaults | None = None) -> dict[str, object]:
    """Merge a camera food with shelf defaults into the fields a shelf row needs to reorder."""
    defaults = defaults or load_shelf_defaults()
    return {
        "reorder_quantity": food.reorder_quantity if food.reorder_quantity is not None else defaults.reorder_quantity,
        "unit": food.unit or defaults.unit,
        "usual_quantity": food.usual_quantity if food.usual_quantity is not None else defaults.usual_quantity,
        "lead_time_days": food.lead_time_days if food.lead_time_days is not None else defaults.lead_time_days,
        "constraints": list(food.constraints or defaults.constraints),
        "preferences": list(food.preferences or defaults.preferences),
        "search_terms": (food.search_terms or defaults.search_terms or food.name).strip(),
    }
