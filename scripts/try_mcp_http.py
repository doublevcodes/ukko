"""Connect to the *network* MCP server as an outside agent (streamable HTTP).

Start the MCP server first (gateway on :8001 must already be running):

    python -m gateway.mcp_server

Then:

    python scripts/try_mcp_http.py
    python scripts/try_mcp_http.py http://127.0.0.1:8002/mcp
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

DEFAULT_URL = "http://127.0.0.1:8002/mcp"


def _data(result) -> dict:
    return json.loads(result.content[0].text)


async def main() -> None:
    url = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL).rstrip("/")
    async with streamable_http_client(url) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print(f"1. Connected to {url}")
        print("   Tools:", ", ".join(tool.name for tool in tools.tools))

        found = _data(await session.call_tool("search_products", {"query": "oat drink", "claims": ["VEGAN"], "limit": 5}))
        print(f"2. search_products('oat drink', VEGAN): {len(found.get('results') or [])} products")
        for product in found.get("results") or []:
            print(f"     {product['id']}  {product['name']}")

        needed_by = (date.today() + timedelta(days=3)).isoformat()
        ids = [product["id"] for product in (found.get("results") or [])]
        if ids:
            offers = _data(await session.call_tool("get_offers", {"product_ids": ids, "needed_by": needed_by}))
            print(f"3. get_offers for delivery by {needed_by}: {len(offers.get('offers') or [])} offers")
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
