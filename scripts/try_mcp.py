"""Connect to the gateway's MCP server as an outside agent would, and look up a product.

Starts the MCP server itself (over stdio). The gateway must be running:
    uvicorn gateway.main:app --port 8001

Run:  python scripts/try_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


def _data(result) -> dict:
    """The JSON a tool returned (the first text block of the MCP result)."""
    return json.loads(result.content[0].text)


async def main() -> None:
    server = StdioServerParameters(command=sys.executable, args=["-m", "gateway.mcp_server"], cwd=str(ROOT),
                                   env={"MCP_AGENT_ID": "try_mcp.py (outside agent)"})
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print("1. Connected over MCP. Tools offered:", ", ".join(tool.name for tool in tools.tools))

        found = _data(await session.call_tool("search_products", {"query": "oat drink", "claims": ["VEGAN"], "limit": 5}))
        print(f"2. search_products('oat drink', VEGAN): {len(found['results'])} products")
        for product in found["results"]:
            print(f"     {product['id']}  {product['name']}")

        needed_by = (date.today() + timedelta(days=3)).isoformat()
        ids = [product["id"] for product in found["results"]]
        offers = _data(await session.call_tool("get_offers", {"product_ids": ids, "needed_by": needed_by}))
        print(f"3. get_offers for delivery by {needed_by}:")
        for offer in offers["offers"]:
            state = f"{offer['unit_price']} EUR, delivery {offer['earliest_delivery']}" if offer["orderable"] else offer["not_orderable_reason"]
            print(f"     {offer['product_id']}  {offer['name'][:45]:45}  {state}")

        missing = _data(await session.call_tool("get_order_status", {"order_id": "VA-00000000-0000"}))
        print("4. get_order_status for an order that does not exist:", missing)


if __name__ == "__main__":
    asyncio.run(main())
