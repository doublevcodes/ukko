"""Gateway configuration, read once from the environment (.env)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _flag(name: str, default: bool) -> bool:
    """Read a true/false setting from the environment."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# Everything the gateway writes (keys, order ledger, logs, downloads) goes here.
DATA_DIR = Path(os.getenv("STOCKPOT_DATA_DIR") or ROOT / "data")
GATEWAY_DATA_DIR = DATA_DIR / "gateway"

VALIO_BASE_URL = os.getenv("VALIO_BASE_URL", "").rstrip("/")
VALIO_API_KEY = os.getenv("VALIO_API_KEY", "")
# The mock API ignores the customer number, but the field is required.
VALIO_CUSTOMER = os.getenv("VALIO_CUSTOMER", "123")

# The catalogue file: the downloaded copy first, then the one from the portal.
CATALOGUE_PATHS = [
    DATA_DIR / "products.json",
    ROOT / "valio_aimo_junction26_product_data.json",
]
RELIABILITY_CSV = DATA_DIR / "delivery_reliability.csv"
CARBON_CSV = DATA_DIR / "carbon_footprint.csv"

# See .env.example: an open question for the Valio mentors.
ENFORCE_ORDER_CUTOFF = _flag("ENFORCE_ORDER_CUTOFF", True)

MERCHANT_ID = "valio-aimo-gateway"
MERCHANT_NAME = "Valio Aimo (Junction 2026 mock)"
CURRENCY = "EUR"
# Default JWT audience when the buyer does not name itself on /offers.
DEFAULT_OFFER_AUDIENCE = os.getenv("DEFAULT_OFFER_AUDIENCE", "stockpot-demo-kitchen")

# How long a signed offer or a signed checkout stays valid (wall clock, not the reorder demo clock).
OFFER_TTL_SECONDS = int(os.getenv("OFFER_TTL_SECONDS") or 600)
CHECKOUT_TTL_SECONDS = 600

# Valio asks agents to price only products they have narrowed down.
MAX_OFFER_PRODUCTS = 20
# Valio reports "not limited" stock as 999999 or larger filler numbers.
UNLIMITED_STOCK_FROM = 999_999
