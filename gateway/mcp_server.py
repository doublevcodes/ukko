"""The gateway's interface as MCP tools, so any outside AI agent can use it.

A thin wrapper: every tool is one HTTP call to the running gateway, the same
calls Stockpot makes. It is not in Stockpot's own path.

Run (the gateway must be running on port 8001):
    python -m gateway.mcp_server                 # stdio, for desktop AI apps
    python -m gateway.mcp_server streamable-http # over HTTP
"""

from __future__ import annotations

import os
import sys
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8001").rstrip("/")
AGENT_ID = os.getenv("MCP_AGENT_ID", "outside-agent-via-mcp")

server = MCPServer(
    name="valio-aimo-agent-gateway",
    instructions=(
        "Tools for buying from Valio Aimo's wholesale catalogue (Junction 2026 mock data). "
        "Search first, then ask for offers on a short list. Prices exclude VAT. Quantities are whole sales units. "
        "Ordering needs AP2 mandates signed with a key the buyer has registered."
    ),
)


def _call(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """One call to the gateway. Refusals come back as data, so the agent can read the reason."""
    try:
        response = httpx.request(method, f"{GATEWAY_URL}{path}", timeout=30, headers={"X-Agent-Id": AGENT_ID}, **kwargs)
        return response.json()
    except (httpx.HTTPError, ValueError) as error:
        return {"error": "gateway_unreachable", "message": f"The gateway at {GATEWAY_URL} did not answer ({type(error).__name__})."}


@server.tool()
def search_products(query: str, claims: list[str] | None = None, labels: list[str] | None = None, limit: int = 10) -> dict[str, Any]:
    """Search the catalogue by keywords, for example "oat drink" or "butter".

    `claims` (for example ["FREE_FROM_LACTOSE"]) and `labels` (for example
    ["EU_ORGANIC_FARMING"]) are hard filters. Results carry no price or stock:
    call get_offers for that. Use list_requirement_ids to see valid ids.
    """
    params = {"q": query, "claims": claims or [], "labels": labels or [], "limit": limit}
    return _call("GET", "/products/search", params=params)


@server.tool()
def list_requirement_ids() -> dict[str, Any]:
    """The claim and label ids that search_products accepts as filters."""
    return _call("GET", "/vocabulary")


@server.tool()
def get_offers(product_ids: list[str], needed_by: str) -> dict[str, Any]:
    """Live price, stock and earliest delivery for up to 20 products.

    `needed_by` is the wanted delivery date as YYYY-MM-DD. Each offer is signed
    by the supplier (`offer_jwt`) and expires after ten minutes.
    """
    return _call("POST", "/offers", json={"product_ids": product_ids, "needed_by": needed_by})


@server.tool()
def create_checkout(buyer_id: str, request_id: str, needed_by: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Re-price a cart live and get a supplier-signed checkout to approve.

    `lines` is a list of {"product_id": "...", "quantity": whole sales units}.
    `request_id` is the buyer's own id for this purchase: one id, one order.
    The buyer must have registered an approval key with the supplier.
    """
    body = {"buyer_id": buyer_id, "request_id": request_id, "needed_by": needed_by, "lines": lines}
    return _call("POST", "/checkouts", json=body)


@server.tool()
def complete_checkout(checkout_id: str, checkout_mandate: str, payment_mandate: str) -> dict[str, Any]:
    """Place the order by presenting AP2 mandates for a checkout.

    The supplier verifies the signatures and limits. If they hold, it returns
    the order with signed receipts; otherwise it says which rule was broken.
    """
    body = {"checkout_mandate": checkout_mandate, "payment_mandate": payment_mandate}
    return _call("POST", f"/checkouts/{checkout_id}/complete", json=body)


@server.tool()
def get_order_status(order_id: str) -> dict[str, Any]:
    """Look up an order by its id, for example "VA-20261113-0001"."""
    return _call("GET", f"/orders/{order_id}")


if __name__ == "__main__":
    server.run(transport=sys.argv[1] if len(sys.argv) > 1 else "stdio")
