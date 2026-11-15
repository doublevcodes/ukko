"""Read and write the files in config/: policy rules, shelf defaults, and camera foods."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from app import settings
from app.models import PolicyConfig, Unit

POLICY_HEADER = (
    "# Spending and approval rules. Saved from the dashboard.\n"
    "# auto_approve_max_total_eur: orders above this total (EUR, excluding VAT) need a person.\n"
    "# max_quantity_multiplier_vs_usual: more than this many times the usual amount needs a person.\n"
    "# allowed_categories: product groups the agent may buy on its own (names from categories.yaml).\n"
    "# agent_budget_eur: the most the agent may spend in total on its own; the supplier enforces it.\n"
)

ServiceClass = Literal["critical", "standard", "perishable"]

# Default z scores by service class (REORDER_SPEC.md §3): stock-out risk before delivery.
_DEFAULT_Z = {"critical": 2.326, "standard": 1.645, "perishable": 1.282}


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
    full_quantity: Decimal | None = None  # how much a full shelf holds; defaults to reorder_quantity
    low_quantity: Decimal | None = None  # how much is left at "low"; defaults to a quarter of full
    reorder_below: Decimal | None = None  # order when the camera sees this many units or fewer
    cover_days: int = 7  # days an order should last after it arrives
    typical_daily_use: Decimal | None = None  # expected use per day, until real usage has been measured


class ReorderModelConfig(BaseModel):
    """Global usage / reorder-point parameters (config/shelf.yaml → reorder_model).

    See REORDER_SPEC.md §3. Not yet wired into the live reorder decision (R4).
    """

    model_day_seconds: float = 86400
    alpha: float = 0.3
    sigma_factor: float = 1.5
    z: dict[str, float] = Field(default_factory=lambda: dict(_DEFAULT_Z))
    default_cover_days: int = 7
    default_lead_days: int = 2
    multiplier_min: float = 0.7
    multiplier_max: float = 1.5
    extra_lead_days_min: int = 0
    extra_lead_days_max: int = 3

    def z_for(self, service_class: ServiceClass | str) -> float:
        """Safety-buffer z for a product's service class; unknown classes use standard."""
        return float(self.z.get(service_class, self.z.get("standard", _DEFAULT_Z["standard"])))


class ShelfDefaults(BaseModel):
    """Reorder settings for camera-recognised foods that do not override them."""

    reorder_quantity: Decimal = Decimal(24)
    unit: Unit = "pcs"
    usual_quantity: Decimal | None = Decimal(24)
    lead_time_days: int = 2
    search_terms: str = ""
    constraints: list[str] = []
    preferences: list[str] = []
    full_quantity: Decimal | None = None
    low_quantity: Decimal | None = None
    reorder_below: Decimal = Decimal(2)
    cover_days: int = 7
    typical_daily_use: Decimal | None = None
    service_class: ServiceClass = "standard"
    units_per_visible_item: Decimal = Decimal(1)
    pack_size: Decimal = Decimal(24)
    max_stock_units: Decimal = Decimal(144)
    shelf_life_days: int | None = None


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
    full_quantity: Decimal | None = None
    low_quantity: Decimal | None = None
    reorder_below: Decimal | None = None
    cover_days: int | None = None
    typical_daily_use: Decimal | None = None
    service_class: ServiceClass | None = None
    units_per_visible_item: Decimal | None = None
    pack_size: Decimal | None = None
    max_stock_units: Decimal | None = None
    shelf_life_days: int | None = None

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


def load_reorder_model() -> ReorderModelConfig:
    """Global reorder-model parameters from config/shelf.yaml → reorder_model."""
    return ReorderModelConfig(**(_shelf_file().get("reorder_model") or {}))


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
        "full_quantity": food.full_quantity if food.full_quantity is not None else defaults.full_quantity,
        "low_quantity": food.low_quantity if food.low_quantity is not None else defaults.low_quantity,
        "reorder_below": food.reorder_below if food.reorder_below is not None else defaults.reorder_below,
        "cover_days": food.cover_days if food.cover_days is not None else defaults.cover_days,
        "typical_daily_use": food.typical_daily_use if food.typical_daily_use is not None else defaults.typical_daily_use,
        "service_class": food.service_class if food.service_class is not None else defaults.service_class,
        "units_per_visible_item": (
            food.units_per_visible_item if food.units_per_visible_item is not None
            else defaults.units_per_visible_item
        ),
        "pack_size": food.pack_size if food.pack_size is not None else defaults.pack_size,
        "max_stock_units": food.max_stock_units if food.max_stock_units is not None else defaults.max_stock_units,
        "shelf_life_days": food.shelf_life_days if food.shelf_life_days is not None else defaults.shelf_life_days,
    }

CAMERA_FOODS_HEADER = (
    "# Foods the shelf camera may see. Vision may only label keys from this list.\n"
    "# product_id      optional Valio catalogue id; omit when there is no exact SKU\n"
    "# search_terms    keywords used to find the closest catalogue match when ordering\n"
    "# reorder_below   order when the camera sees this many units or fewer\n"
    "# reorder_quantity how many units to order when a reorder is triggered\n"
    "# typical_daily_use expected units used per day, until real usage is measured\n"
)


def _plain_yaml_value(value: object) -> object:
    """Write whole Decimals as ints so camera_foods.yaml stays readable."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_plain_yaml_value(item) for item in value]
    return value


def update_camera_food_reorder(product_key: str, *, reorder_below: Decimal, reorder_quantity: Decimal) -> CameraFood:
    """Save per-item reorder settings into config/camera_foods.yaml and return the updated food."""
    if not settings.CAMERA_FOODS_FILE.exists():
        raise KeyError(product_key)
    raw = yaml.safe_load(settings.CAMERA_FOODS_FILE.read_text(encoding="utf-8")) or {}
    foods = raw.get("foods") or {}
    if product_key not in foods:
        raise KeyError(product_key)
    entry = dict(foods[product_key] or {})
    entry["reorder_below"] = _plain_yaml_value(reorder_below)
    entry["reorder_quantity"] = _plain_yaml_value(reorder_quantity)
    # Keep usual_quantity in step so the "unusually large" rule stays meaningful.
    entry["usual_quantity"] = _plain_yaml_value(reorder_quantity)
    foods[product_key] = entry
    raw["foods"] = foods
    body = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    settings.CAMERA_FOODS_FILE.write_text(CAMERA_FOODS_HEADER + "\n" + body, encoding="utf-8")
    return CameraFood(**entry)
