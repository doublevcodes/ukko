"""The contracts everything else is built against (SPEC.md Section 7).

Money is Decimal, always excluding VAT. Dates are Europe/Helsinki.
Fields marked "extension" were added to the original contracts for AP2,
signed offers, delivery reliability, carbon footprint and substitute tiers.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

Level = Literal["full", "low", "empty"]
Unit = Literal["l", "kg", "pcs"]  # pcs = sales units exactly as the supplier sells them
Severity = Literal["none", "caution", "blocker", "check_failed"]
Status = Literal["working", "needs_clarification", "awaiting_approval", "ordered", "rejected", "failed"]
Tier = Literal["best", "better", "good"]


class SensorEvent(BaseModel):
    product_key: str = Field(description="Matches a key in config/shelf.yaml.")
    level: Level
    confidence: float = Field(ge=0, le=1)
    timestamp: datetime
    device_id: str


class OrderRequest(BaseModel):
    id: str
    source: Literal["sensor", "typed"]
    raw_text: str | None = None
    item_description: str
    quantity: Decimal
    unit: Unit
    needed_by: date
    constraints: list[str] = []  # hard requirements, e.g. ["FREE_FROM_LACTOSE"]
    preferences: list[str] = []  # soft, e.g. ["EU_ORGANIC_FARMING"]
    max_unit_price: Decimal | None = None  # per litre, kilo or piece, matching `unit`
    search_terms: str = ""  # extension: keywords for the catalogue search
    product_id: str | None = None  # extension: the exact product, known for shelf items
    usual_quantity: Decimal | None = None  # extension: the normal reorder amount, if known


class Offer(BaseModel):
    """One supplier answer for one product: live price and availability, signed."""

    product_id: str
    name: str
    unit_price: Decimal | None  # per sales unit; None when the supplier has no price
    currency: str = "EUR"
    pack_size: Decimal | None = None  # litres or kilos in one sales unit
    pack_unit: Literal["L", "KG"] | None = None
    available_quantity: int | None = None  # sales units; None means not limited
    earliest_delivery: date | None = None
    attributes: list[str] = []  # claims such as FREE_FROM_LACTOSE
    labels: list[str] = []  # certificates such as EU_ORGANIC_FARMING
    offer_id: str = ""  # the supplier's quote id
    expires_at: datetime | None = None
    offer_jwt: str = ""  # extension: the supplier's signature over this offer
    # Extensions below.
    sales_unit: str = ""
    comparison_price: Decimal | None = None  # per litre or kilo
    orderable: bool = False
    not_orderable_reason: str | None = None
    order_cutoff: datetime | None = None
    brand: str | None = None
    category: str = ""
    category_name: str | None = None  # from config/categories.yaml; None if unmapped
    listed_substitutes: list[str] = []
    delivery_reliability: float | None = None  # 0 to 1
    co2_kg_per_kg: float | None = None
    kg_per_sales_unit: Decimal | None = None


class PlanLine(BaseModel):
    offer: Offer
    quantity: int  # sales units
    line_total: Decimal
    is_substitute: bool = False
    substitute_reason: str | None = None
    substitute_tier: Tier | None = None  # extension
    covers: Decimal = Decimal(0)  # extension: how much of the request this line covers, in the request's unit


class Plan(BaseModel):
    id: str  # "A", "B" or "C"
    lines: list[PlanLine]
    total: Decimal
    meets_deadline: bool
    meets_constraints: bool
    coverage: Decimal  # fraction of the requested quantity, 0 to 1
    notes: list[str] = []
    label: str = ""  # extension: one-line description
    delivery_date: date | None = None  # extension: when the last line arrives
    reliability: float | None = None  # extension: lowest delivery reliability among the lines
    co2_kg: Decimal | None = None  # extension: estimated footprint of the whole plan


class SubstituteOption(BaseModel):
    """Extension: one rung of the good-better-best substitute ranking."""

    tier: Tier
    product_id: str
    name: str
    comparison_price: Decimal | None = None
    reason: str
    used_in_plan: bool = False


class Source(BaseModel):
    title: str
    url: str
    published_date: str | None = None
    snippet: str = Field(default="", max_length=200)


class WebFinding(BaseModel):
    check_type: Literal["recall", "substitute_suitability", "price_sanity", "other"]
    query: str
    summary: str
    severity: Severity
    sources: list[Source] = []
    product_id: str | None = None  # extension: which product this is about


class PolicyResult(BaseModel):
    outcome: Literal["auto_approve", "needs_approval", "deny"]
    reasons: list[str]


class OrderConfirmation(BaseModel):
    order_id: str
    total: Decimal
    currency: str = "EUR"
    placed_at: datetime
    lines: list[dict[str, Any]] = []
    # AP2: how the order was authorised, and the supplier's signed receipts.
    flow: Literal["human_present", "human_not_present"]
    mandate_reference: str
    checkout_receipt_jwt: str
    payment_receipt_jwt: str
    receipts_verified: bool = False
    payment_method: str = ""


class Alternative(BaseModel):
    plan: Plan
    why_not: str


class Decision(BaseModel):
    request_id: str
    chosen_plan: Plan | None = None
    alternatives: list[Alternative] = []
    explanation: str = ""
    web_findings: list[WebFinding] = []
    policy_result: PolicyResult | None = None
    status: Status = "working"
    order_confirmation: OrderConfirmation | None = None
    substitute_options: list[SubstituteOption] = []  # extension
    message: str | None = None  # extension: clarifying question or failure reason


class AuditEntry(BaseModel):
    timestamp: datetime
    request_id: str | None
    step: str
    summary: str
    details: dict[str, Any] = {}


class PolicyConfig(BaseModel):
    """config/policy.yaml. Every field can be edited from the dashboard."""

    auto_approve_max_total_eur: Decimal = Field(default=Decimal(500), ge=0)
    max_quantity_multiplier_vs_usual: Decimal = Field(default=Decimal(3), gt=0)
    allowed_categories: list[str] = ["dairy", "plant-based drinks"]
    substitutes_require_approval: bool = True
    late_delivery_requires_approval: bool = True
    web_caution_requires_approval: bool = True
    web_check_failed_requires_approval: bool = True
    # AP2: the most the agent may spend in total on its own before a person
    # must renew its authority. Enforced by the supplier, not by policy.py.
    agent_budget_eur: Decimal = Field(default=Decimal(2000), ge=0)
