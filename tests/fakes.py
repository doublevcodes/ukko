"""Stand-ins for the outside world, so tests never use the network.

FakeValio  answers like Valio's mock API (availability and price only)
FakeLLM    answers like app/llm.py
FakeExa    answers like one Exa search
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app import llm

HELSINKI = ZoneInfo("Europe/Helsinki")
UNLIMITED = 999999  # what Valio sends when stock is not limited

# id: (name, brand, category, claims, labels, size of one sales unit, comparison unit, price per sales unit)
PRODUCTS: dict[str, tuple] = {
    "1001": ("Arla semi skimmed milk 1 L lactose free", "Arla", "10400", ["FREE_FROM_LACTOSE"], [], 1.0, "L", 2.00),
    "1002": ("Valio semi skimmed milk 1 l lactose free", "Valio", "10400", ["FREE_FROM_LACTOSE"], [], 1.0, "L", 1.50),
    "1003": ("Arla whole milk 1 L lactose free", "Arla", "10400", ["FREE_FROM_LACTOSE"], [], 1.0, "L", 2.20),
    "1004": ("Valio semi skimmed milk 1 l", "Valio", "10400", [], [], 1.0, "L", 1.00),
    "2001": ("Valio butter 500 g", "Valio", "10500", [], [], 0.5, "KG", 5.00),
    "2002": ("Valio Luomu butter 500 g", "Valio", "10500", [], ["EU_ORGANIC_FARMING"], 0.5, "KG", 6.00),
}


def catalogue_file_content() -> str:
    """The fixture products in the shape of Valio's catalogue download."""
    entries = []
    for product_id, (name, brand, category, claims, labels, size, unit, _price) in PRODUCTS.items():
        measure = {"volume": {"value": size, "unit": "LTR"}} if unit == "L" else {}
        entries.append({
            "id": product_id, "vendorName": f"{brand} Oy", "category": category, "salesUnit": "ST",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1.0}], "countryOfOrigin": "fi",
            "salesUnitGtin": f"640000000{product_id}", "substitutions": [],
            "synkkaData": {
                "names": [{"language": "en", "value": name}], "brand": brand,
                "gtin": f"640000000{product_id}",
                "producers": [{"id": "6407809999995", "name": f"{brand} Oy"}],
                "countriesOfOrigin": ["246"],
                "classifications": [
                    {"name": "nutritionalClaim", "values": [{"id": claim} for claim in claims]},
                    {"name": "packagingMarkedLabel", "values": [{"id": label} for label in labels]},
                ],
                "unitConversions": [{"synkkaPackagingType": "BASE_UNIT_OR_EACH",
                                     "netWeight": {"value": size, "unit": "KGM"}, **measure}],
            },
        })
    return json.dumps(entries)


class FakeValio:
    """Valio's availability and price endpoints, with knobs the tests can turn."""

    def __init__(self) -> None:
        self.down = False
        self.stock: dict[str, int] = {}  # product id -> units in stock (missing = not limited)
        self.prices: dict[str, float] = {key: value[7] for key, value in PRODUCTS.items()}
        self.delivery_in_days = 1
        self.calls = 0
        self.on_call: Any = None  # optional function(call_number), run before each answer

    def _availability(self, ids: list[str]) -> dict[str, Any]:
        now = datetime.now(HELSINKI)
        day = (date.today() + timedelta(days=self.delivery_in_days)).isoformat()
        products = [{
            "productId": product_id, "logisticModel": "Stock", "salesBlock": False,
            "closingTime": (now + timedelta(hours=6)).isoformat(),
            "scheduleLines": [{"availableDate": f"{day}T00:00:00", "availableAmount": self.stock.get(product_id, UNLIMITED),
                               "salesUnit": "ST", "salesBlock": False}] if product_id in PRODUCTS else [],
        } for product_id in ids]
        return {"timeNow": now.isoformat(), "products": products}

    def _price(self, ids: list[str]) -> dict[str, Any]:
        products = []
        for product_id in ids:
            if product_id not in PRODUCTS:
                products.append({"productId": product_id, "error": True})
                continue
            size, unit, price = PRODUCTS[product_id][5], PRODUCTS[product_id][6], self.prices[product_id]
            products.append({
                "productId": product_id, "error": False, "catalogPrice": {"value": price},
                "comparisonPrice": {"value": round(price / size, 4)}, "comparisonUnit": unit, "tax": {"taxRate": 13.5},
            })
        return {"products": products}

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        if self.down:
            return httpx.Response(503, text="Service unavailable")
        ids = json.loads(request.content)["products"]
        if request.url.path == "/api/availability":
            return httpx.Response(200, json=self._availability(ids))
        if request.url.path == "/api/price":
            return httpx.Response(200, json=self._price(ids))
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)


class FakeLLM:
    """Scripted answers for the three kinds of JSON call, and for the explanation."""

    def __init__(self) -> None:
        self.down = False
        self.matches_nothing = False  # "none of the search hits is the requested item"
        self.parse_answer: dict[str, Any] = {}
        self.verdict = {"verdict": "nothing_relevant", "summary": "Nothing useful.", "source_numbers": []}

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        if self.down:
            raise llm.LLMError("The language model is switched off in this test.")
        fields = schema["properties"]
        if "matching_product_ids" in fields and self.matches_nothing:
            return {"matching_product_ids": []}
        if "matching_product_ids" in fields:
            return {"matching_product_ids": [line.split(":")[0] for line in user.split("Products:\n")[1].splitlines()]}
        if "verdict" in fields:
            return self.verdict
        return self.parse_answer

    def complete_text(self, system: str, user: str) -> str:
        if self.down:
            raise llm.LLMError("The language model is switched off in this test.")
        return "The plan was chosen because it ranked best."


class FakeExa:
    """One Exa search: returns canned results, or fails."""

    def __init__(self) -> None:
        self.down = False
        self.results: list[dict[str, Any]] = []
        self.queries: list[str] = []

    def search(self, query: str, since: date | None) -> list[dict[str, Any]]:
        self.queries.append(query)
        if self.down:
            raise ConnectionError("Exa is switched off in this test.")
        return self.results


class FakeStripe:
    """Stripe setup + off-session charge, with knobs the tests can turn."""

    def __init__(self) -> None:
        self.down = False
        self.decline = False
        self.charges: list[dict[str, Any]] = []
        self._customers = 0
        self._sessions: dict[str, str] = {}  # session_id -> customer_id

    def configured(self) -> bool:
        return not self.down

    def create_customer(self, name: str) -> str:
        self._customers += 1
        return f"cus_test_{self._customers}"

    def create_embedded_setup(self, customer_id: str, return_url: str) -> dict[str, str]:
        session_id = f"cs_test_{len(self._sessions) + 1}"
        self._sessions[session_id] = customer_id
        return {"client_secret": f"cs_test_secret_{session_id}", "session_id": session_id}

    def setup_payment_method(self, session_id: str) -> tuple[str, str, dict[str, Any]]:
        from app.payments import PaymentError
        customer_id = self._sessions.get(session_id)
        if not customer_id:
            raise PaymentError("Stripe could not find that Checkout session.")
        return customer_id, "pm_test_visa", {"brand": "Visa", "last4": "4242"}
    def set_default_payment_method(self, customer_id: str, payment_method_id: str) -> None:
        return None

    def charge(self, customer_id: str, payment_method_id: str, amount_cents: int,
               currency: str, request_id: str, description: str) -> str:
        from app.payments import PaymentError
        if self.decline:
            raise PaymentError("The kitchen card was declined (card_declined). "
                               "Open Change card in Settings if you need to use another card.")
        payment_id = f"pi_test_{len(self.charges) + 1}"
        self.charges.append({
            "id": payment_id, "customer_id": customer_id, "payment_method_id": payment_method_id,
            "amount_cents": amount_cents, "currency": currency, "request_id": request_id,
        })
        return payment_id
