"""The gateway's interface as MCP tools, so any outside AI agent can use it.

A thin wrapper: every tool is one HTTP call to the running gateway, the same
calls Ukko makes. It is not in Ukko's own path.

Run (the gateway must be running on port 8001):

    # Network: other agents on the laptop / LAN (default)
    python -m gateway.mcp_server
    # → http://0.0.0.0:8002/mcp

    # Desktop apps that spawn a local process (Cursor stdio config)
    python -m gateway.mcp_server stdio

Env: GATEWAY_URL, MCP_HOST (default 0.0.0.0), MCP_PORT (default 8002),
MCP_PATH (default /mcp), MCP_AGENT_ID.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8001").rstrip("/")
AGENT_ID = os.getenv("MCP_AGENT_ID", "outside-agent-via-mcp")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT") or 8002)
MCP_PATH = os.getenv("MCP_PATH", "/mcp")

server = MCPServer(
    name="valio-aimo-agent-gateway",
    instructions=(
        "Tools for buying from Valio Aimo's wholesale catalogue (Junction 2026 mock data). "
        "Search first, then ask for offers on a short list. Prices exclude VAT. Quantities are whole sales units. "
        "Ordering needs AP2 mandates signed with a key the buyer has registered. "
        "The same HTTP API is at GATEWAY_URL /docs; this MCP server is that API as tools."
    ),
    website_url=GATEWAY_URL,
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
def get_offers(product_ids: list[str], needed_by: str, buyer_id: str | None = None) -> dict[str, Any]:
    """Live price, stock and earliest delivery for up to 20 products.

    `needed_by` is the wanted delivery date as YYYY-MM-DD. Each offer is signed
    by the supplier (`offer_jwt`) and expires after ten minutes. The response
    includes the signed token next to the readable fields. `buyer_id` becomes
    the JWT audience (defaults to the demo kitchen).
    """
    body: dict[str, Any] = {"product_ids": product_ids, "needed_by": needed_by}
    if buyer_id:
        body["buyer_id"] = buyer_id
    return _call("POST", "/offers", json=body)


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
def complete_checkout(
    checkout_id: str, checkout_mandate: str, payment_mandate: str,
    stripe_payment_intent_id: str | None = None, payment_method_label: str | None = None,
) -> dict[str, Any]:
    """Place the order by presenting AP2 mandates for a checkout.

    The supplier verifies the signatures and limits. If they hold, it returns
    the order with signed receipts; otherwise it says which rule was broken.
    Pass a Stripe PaymentIntent id when the buyer already charged a card.
    """
    body: dict[str, Any] = {"checkout_mandate": checkout_mandate, "payment_mandate": payment_mandate}
    if stripe_payment_intent_id:
        body["stripe_payment_intent_id"] = stripe_payment_intent_id
    if payment_method_label:
        body["payment_method_label"] = payment_method_label
    return _call("POST", f"/checkouts/{checkout_id}/complete", json=body)


@server.tool()
def get_order_status(order_id: str) -> dict[str, Any]:
    """Look up an order by its id, for example "VA-20261113-0001"."""
    return _call("GET", f"/orders/{order_id}")


def _public_url() -> str:
    host = "127.0.0.1" if MCP_HOST in ("0.0.0.0", "::") else MCP_HOST
    return f"http://{host}:{MCP_PORT}{MCP_PATH}"


if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "streamable-http"
    if transport == "stdio":
        server.run(transport="stdio")
    elif transport in ("streamable-http", "http", "sse"):
        kind = "sse" if transport == "sse" else "streamable-http"
        print(f"Valio Aimo MCP ({kind}) for outside agents", flush=True)
        print(f"  Gateway API:  {GATEWAY_URL}/docs", flush=True)
        print(f"  MCP endpoint: {_public_url()}", flush=True)
        print("  Cursor / Claude: add an MCP server with that URL (streamable HTTP).", flush=True)
        server.run(transport=kind, host=MCP_HOST, port=MCP_PORT, streamable_http_path=MCP_PATH)
    else:
        print(f"Unknown transport '{transport}'. Use streamable-http (default) or stdio.", file=sys.stderr)
        sys.exit(2)
