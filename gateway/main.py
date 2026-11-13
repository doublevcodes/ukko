"""Valio agent gateway: the interface an outside purchasing agent uses.

Run:  uvicorn gateway.main:app --port 8001
The interactive description of every endpoint is at http://localhost:8001/docs
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from gateway import catalogue, keys, ledger, merchant, offers, settings, supplier_log
from gateway.valio_client import ValioError

AgentId = Annotated[str | None, Header(description="Who is asking, for the supplier's log. Self-declared.")]


class OffersRequest(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=settings.MAX_OFFER_PRODUCTS,
                                   description="Products to price. Narrow down with search first.")
    needed_by: date = Field(description="The delivery date the buyer wants.")


class BuyerRegistration(BaseModel):
    buyer_id: str = Field(min_length=1)
    public_jwk: dict[str, Any] = Field(description="Public key the buyer's approvals are signed with (P-256 JWK).")


class CheckoutLine(BaseModel):
    product_id: str
    quantity: int = Field(ge=1, description="Whole sales units.")


class CheckoutRequest(BaseModel):
    buyer_id: str
    request_id: str = Field(description="The buyer's own id for this purchase. One request, one order.")
    needed_by: date
    lines: list[CheckoutLine] = Field(min_length=1, max_length=settings.MAX_OFFER_PRODUCTS)


class CompleteRequest(BaseModel):
    checkout_mandate: str = Field(description="AP2 checkout mandate: one signed token, or an open~~closed chain.")
    payment_mandate: str = Field(description="AP2 payment mandate in the same form.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    count = catalogue.load()
    ledger.load()
    keys.supplier_key()
    supplier_log.record("startup", "gateway", f"Gateway started with {count:,} products in the catalogue.")
    yield


app = FastAPI(title="Valio Aimo agent gateway (Junction 2026 prototype)", version="0.1.0", lifespan=lifespan)


@app.exception_handler(ValioError)
async def valio_down(_request: Request, error: ValioError) -> JSONResponse:
    supplier_log.record("valio_error", "gateway", f"Valio's API could not answer: {error}")
    return JSONResponse(status_code=502, content={"error": "valio_unavailable", "message": str(error)})


@app.exception_handler(merchant.CheckoutError)
async def checkout_refused(_request: Request, error: merchant.CheckoutError) -> JSONResponse:
    content = {"error": error.code, "message": error.message, "detail": error.detail}
    return JSONResponse(status_code=error.status, content=content)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "valio-agent-gateway", "products": catalogue.count(),
            "merchant": {"id": settings.MERCHANT_ID, "name": settings.MERCHANT_NAME}}


@app.get("/.well-known/jwks.json")
def public_keys() -> dict[str, Any]:
    """Public keys for checking the supplier's signature on offers, checkouts and receipts."""
    return keys.jwks()


@app.get("/vocabulary")
def vocabulary() -> dict[str, Any]:
    """The claims (for example FREE_FROM_LACTOSE) and labels usable as search filters."""
    return catalogue.vocabulary()


@app.get("/products/search")
def search_products(
    q: str = "", claims: Annotated[list[str], Query()] = [], labels: Annotated[list[str], Query()] = [],  # noqa: B006
    category: str | None = None, limit: Annotated[int, Query(ge=1, le=50)] = 20, x_agent_id: AgentId = None,
) -> dict[str, Any]:
    """Search the catalogue by keywords. Claims and labels are hard filters. No prices: ask /offers."""
    found = catalogue.search(q, claims, labels, category, limit)
    filters = ", ".join(claims + labels) or "no filters"
    supplier_log.record("search", x_agent_id, f"Searched for '{q}' ({filters}): {len(found)} products.")
    return {"results": [product.model_dump() for product in found]}


@app.get("/products/{product_id}")
def get_product(product_id: str) -> dict[str, Any]:
    product = catalogue.get(product_id)
    if product is None:
        raise HTTPException(404, "No such product.")
    return product.model_dump()


@app.post("/offers")
def get_offers(body: OffersRequest, x_agent_id: AgentId = None) -> dict[str, Any]:
    """Live price and availability for up to 20 products. Every offer is signed and has an expiry."""
    result = offers.get_offers(body.product_ids, body.needed_by)
    supplier_log.record("offers", x_agent_id, f"Priced {len(body.product_ids)} products for delivery by {body.needed_by}.",
                        {"product_ids": body.product_ids})
    return result


@app.post("/buyers")
def register_buyer(body: BuyerRegistration, x_agent_id: AgentId = None) -> dict[str, Any]:
    """Register the key a buyer signs approvals with. A stand-in for customer onboarding."""
    try:
        new = ledger.register_buyer(body.buyer_id, body.public_jwk)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if new:
        supplier_log.record("buyer_registered", x_agent_id, f"Buyer '{body.buyer_id}' registered an approval key.")
    return {"buyer_id": body.buyer_id, "registered": True}


@app.post("/checkouts")
def create_checkout(body: CheckoutRequest, x_agent_id: AgentId = None) -> dict[str, Any]:
    """Re-price a cart live and return a supplier-signed checkout to approve."""
    lines = [line.model_dump() for line in body.lines]
    try:
        result = merchant.create_checkout(body.buyer_id, body.request_id, body.needed_by, lines)
    except merchant.CheckoutError as error:
        supplier_log.record("checkout_refused", x_agent_id, f"Refused a checkout for request {body.request_id}: {error.message}",
                            {"detail": error.detail})
        raise
    if "checkout" in result:
        total = result["checkout"]["totals"][1]["amount"] / 100
        supplier_log.record("checkout_created", x_agent_id,
                            f"Signed checkout {result['checkout_id']} for {total:.2f} EUR ({len(lines)} lines).")
    return result


@app.post("/checkouts/{checkout_id}/complete")
def complete_checkout(checkout_id: str, body: CompleteRequest, x_agent_id: AgentId = None) -> dict[str, Any]:
    """Present AP2 mandates for a checkout. If they hold, the order is recorded and receipts returned."""
    try:
        order = merchant.complete_checkout(checkout_id, body.checkout_mandate, body.payment_mandate)
    except merchant.CheckoutError as error:
        reasons = " ".join((error.detail or {}).get("violations", [])) if isinstance(error.detail, dict) else ""
        supplier_log.record("order_refused", x_agent_id, f"Refused to complete {checkout_id}: {error.message} {reasons}".strip())
        raise
    how = "approved by a person" if order["flow"] == "human_present" else "placed by the agent within its standing limits"
    if not order.get("duplicate"):
        supplier_log.record("order_placed", x_agent_id, f"Order {order['order_id']} for {order['total']} EUR, {how}.",
                            {"order_id": order["order_id"], "buyer_id": order["buyer_id"]})
    return order


@app.get("/orders")
def list_orders() -> dict[str, Any]:
    return {"orders": ledger.all_orders()}


@app.get("/orders/{order_id}")
def get_order(order_id: str) -> dict[str, Any]:
    order = ledger.get_order(order_id)
    if order is None:
        raise HTTPException(404, "No such order.")
    return order


@app.get("/log")
def log(limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict[str, Any]:
    """The supplier's own record of what agents looked up and ordered, newest first."""
    return {"entries": supplier_log.latest(limit)}


@app.get("/", include_in_schema=False)
def supplier_view() -> FileResponse:
    return FileResponse(Path(__file__).parent / "supplier_view.html")
