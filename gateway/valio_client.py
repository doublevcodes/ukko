"""Raw HTTP client for the Valio Aimo mock API: one function per endpoint.

Built strictly from api.json. The API only offers ping, availability, price
and three file downloads. It has no search and no ordering endpoint.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import httpx

from gateway import settings

TIMEOUT_SECONDS = 10
MAX_PRODUCTS_PER_CALL = 1000  # from api.json: longer lists are rejected with 422

_client: httpx.Client | None = None
_transport: httpx.BaseTransport | None = None


class ValioError(Exception):
    """The Valio API could not give a usable answer. The message is readable."""


def use_transport(transport: httpx.BaseTransport | None) -> None:
    """Swap the network layer. Tests use this so they never call the live API."""
    global _client, _transport
    _transport, _client = transport, None


def _get_client() -> httpx.Client:
    global _client
    if not settings.VALIO_BASE_URL or not settings.VALIO_API_KEY:
        raise ValioError("VALIO_BASE_URL and VALIO_API_KEY must be set in .env.")
    if _client is None:
        _client = httpx.Client(
            base_url=settings.VALIO_BASE_URL,
            headers={"X-API-Key": settings.VALIO_API_KEY, "User-Agent": "stockpot-gateway/0.1"},
            timeout=TIMEOUT_SECONDS,
            transport=_transport,
            follow_redirects=True,  # the download endpoints redirect to the file
        )
    return _client


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
    """Send one request, retrying once on network errors, 429 and 5xx."""
    problem = "unknown error"
    for _attempt in (1, 2):
        try:
            response = _get_client().request(method, path, json=body)
        except httpx.TimeoutException:
            problem = f"no answer within {TIMEOUT_SECONDS} seconds"
            continue
        except httpx.HTTPError as error:
            problem = f"could not connect ({type(error).__name__})"
            continue
        if response.status_code == 429 or response.status_code >= 500:
            problem = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise ValioError(f"Valio API refused {method} {path}: HTTP {response.status_code} {response.text[:200]}")
        return response
    raise ValioError(f"Valio API is unavailable ({method} {path}): {problem}, after one retry.")


def _products_body(product_ids: list[str], requested_date: date) -> dict[str, Any]:
    if not 1 <= len(product_ids) <= MAX_PRODUCTS_PER_CALL:
        raise ValioError(f"Valio accepts 1 to {MAX_PRODUCTS_PER_CALL} products per call, got {len(product_ids)}.")
    return {
        "customer": settings.VALIO_CUSTOMER,
        "requestedDate": requested_date.isoformat(),
        "products": product_ids,
    }


def ping() -> dict[str, Any]:
    """GET /api/ping: check the API is up and the key is accepted."""
    return _request("GET", "/api/ping").json()


def availability(product_ids: list[str], requested_date: date) -> dict[str, Any]:
    """POST /api/availability: can these products be delivered, and how many."""
    return _request("POST", "/api/availability", _products_body(product_ids, requested_date)).json()


def price(product_ids: list[str], requested_date: date) -> dict[str, Any]:
    """POST /api/price: the price of these products for the requested date."""
    return _request("POST", "/api/price", _products_body(product_ids, requested_date)).json()


def _download(path: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(_request("GET", path).content)
    return destination


def download_products(destination: Path) -> Path:
    """GET /api/products/download: the product catalogue as JSON (about 87 MB)."""
    return _download("/api/products/download", destination)


def download_delivery_reliability(destination: Path) -> Path:
    """GET /api/delivery-reliability/download: CSV of productId, deliveryReliability."""
    return _download("/api/delivery-reliability/download", destination)


def download_carbon_footprint(destination: Path) -> Path:
    """GET /api/carbon-footprint/download: CSV of productId, AVG kg CO2-eq/kg."""
    return _download("/api/carbon-footprint/download", destination)
