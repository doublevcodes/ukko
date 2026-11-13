"""The product catalogue: loaded once and kept as a slim search index.

Valio's API has no search endpoint, so search runs here over the product
file. The file is about 87 MB; only the fields an agent needs are kept, and
the slim version is cached on disk so restarts are fast.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

from pydantic import BaseModel

from gateway import settings

_TEMPERATURE_LABELS = {"warmTemperature", "coolTemperature", "freezeTemperature"}
_VOLUME_TO_LITRES = {"LTR": 1.0, "DLT": 0.1, "CLT": 0.01, "MLT": 0.001}
_WEIGHT_TO_KG = {"KGM": 1.0, "GRM": 0.001}
_WORD = re.compile(r"[a-z0-9äöå]+")
MIN_PRODUCTS_FOR_VOCABULARY = 20


class Product(BaseModel):
    """The static facts about one product. No price or stock: those are live."""

    id: str
    name: str
    name_fi: str | None = None
    brand: str | None = None
    vendor: str
    category: str
    sales_unit: str
    base_units_per_sales_unit: float = 1.0
    base_unit_litres: float | None = None
    base_unit_kg: float | None = None
    claims: list[str] = []
    labels: list[str] = []
    country_of_origin: str | None = None
    listed_substitutes: list[str] = []
    delivery_reliability: float | None = None
    co2_kg_per_kg: float | None = None


_products: dict[str, Product] = {}
_tokens: dict[str, frozenset[str]] = {}  # every searchable word of a product (English, Finnish, brand)
_name_words: dict[str, list[str]] = {}  # the English name's words, in order
_SIZE_WORD = re.compile(r"e?\d")  # "500", "1", "e900": the start of a pack size


def _measure(entry: dict | None, table: dict[str, float]) -> float | None:
    if not entry or entry.get("unit") not in table:
        return None
    return round(entry["value"] * table[entry["unit"]], 6)


def _slim(raw: dict) -> dict:
    """Keep only what an agent needs from one raw catalogue entry."""
    data = raw["synkkaData"]
    names = {n["language"]: n["value"] for n in data.get("names") or []}
    groups = {g["name"]: [v["id"] for v in g["values"]] for g in data.get("classifications") or []}
    sizes = {u["unitId"]: u["sizeInBaseUnits"] for u in raw.get("units") or []}
    conversions = data.get("unitConversions") or []
    each = next((u for u in conversions if u.get("synkkaPackagingType") == "BASE_UNIT_OR_EACH"), {})
    return {
        "id": raw["id"],
        "name": names.get("en") or names.get("fi") or next(iter(names.values()), raw["id"]),
        "name_fi": names.get("fi"),
        "brand": data.get("brand"),
        "vendor": raw["vendorName"],
        "category": raw["category"],
        "sales_unit": raw["salesUnit"],
        "base_units_per_sales_unit": sizes.get(raw["salesUnit"], 1.0),
        "base_unit_litres": _measure(each.get("volume"), _VOLUME_TO_LITRES),
        "base_unit_kg": _measure(each.get("netWeight"), _WEIGHT_TO_KG),
        "claims": groups.get("nutritionalClaim", []),
        "labels": [x for x in groups.get("packagingMarkedLabel", []) if x not in _TEMPERATURE_LABELS],
        "country_of_origin": raw.get("countryOfOrigin"),
        "listed_substitutes": [s["substituteProduct"] for s in raw.get("substitutions") or []],
    }


def _load_slim(source: Path) -> list[dict]:
    """Read the slim index from the cache, rebuilding it if the source changed."""
    cache = settings.DATA_DIR / "catalogue_index.json"
    stamp = [source.stat().st_size, int(source.stat().st_mtime)]
    if cache.exists():
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("stamp") == stamp:
            return cached["products"]
    products = [_slim(raw) for raw in json.loads(source.read_text(encoding="utf-8"))]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"stamp": stamp, "products": products}, ensure_ascii=False), encoding="utf-8")
    return products


def _read_csv(path: Path) -> dict[str, float]:
    """Read a two-column Valio CSV (productId, number). Missing file means no data."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))[1:]
    return {row[0]: float(row[1]) for row in rows if len(row) >= 2}


def _normalise(token: str) -> str:
    """A crude shared form for singular and plural, used on both the query and the names.

    eggs -> egg, potatoes -> potato, berries and berry -> berri, cookies and
    cookie -> cooki. The result is only compared, never shown.
    """
    if len(token) <= 3:
        return token
    if token.endswith("s"):
        token = token[:-1]
    if token.endswith("oe"):
        return token[:-1]
    if token.endswith("ie"):
        return token[:-1]
    if token.endswith("y"):
        return token[:-1] + "i"
    return token


def _tokenise(text: str) -> frozenset[str]:
    return frozenset(_normalise(token) for token in _WORD.findall(text.lower()))


def load(source: Path | None = None) -> int:
    """Load the catalogue into memory. Returns the number of products."""
    source = source or next((p for p in settings.CATALOGUE_PATHS if p.exists()), None)
    if source is None:
        raise FileNotFoundError("No product catalogue found. Run: python scripts/try_valio.py --download")
    reliability = _read_csv(settings.RELIABILITY_CSV)
    carbon = _read_csv(settings.CARBON_CSV)
    _products.clear(), _tokens.clear(), _name_words.clear()
    for entry in _load_slim(source):
        product = Product(
            **entry,
            delivery_reliability=reliability.get(entry["id"]),
            co2_kg_per_kg=carbon.get(entry["id"]),
        )
        _products[product.id] = product
        _tokens[product.id] = _tokenise(f"{product.name} {product.name_fi or ''} {product.brand or ''}")
        _name_words[product.id] = [_normalise(word) for word in _WORD.findall(product.name.lower())]
    return len(_products)


def get(product_id: str) -> Product | None:
    return _products.get(product_id)


def count() -> int:
    return len(_products)


def search(
    query: str = "",
    claims: list[str] | None = None,
    labels: list[str] | None = None,
    category: str | None = None,
    limit: int = 20,
) -> list[Product]:
    """Find products by keywords. Claims and labels are hard filters.

    A product matches when it has at least half of the query words in its
    name. Results in the most common category among the best matches come
    first, so "milk" returns milk before milk chocolate.
    """
    wanted, need_claims, need_labels = _tokenise(query), set(claims or []), set(labels or [])
    matches: list[tuple[float, Product]] = []
    for product in _products.values():
        if category and product.category != category:
            continue
        if not need_claims <= set(product.claims) or not need_labels <= set(product.labels):
            continue
        hits = len(wanted & _tokens[product.id])
        if wanted and hits * 2 < len(wanted):
            continue
        score = hits / len(wanted) if wanted else 1.0
        matches.append((score + (0.5 if _is_the_product(wanted, product.id) else 0.0), product))
    if not matches:
        return []
    best = max(score for score, _ in matches)
    main_category = Counter(p.category for score, p in matches if score == best).most_common(1)[0][0]
    matches.sort(key=lambda m: (-m[0], m[1].category != main_category, len(_tokens[m[1].id]), m[1].id))
    return [product for _, product in matches[:limit]]


def _is_the_product(wanted: frozenset[str], product_id: str) -> bool:
    """True when the search words name the product itself, not a flavour or filling.

    In "Valio butter 500 g" the word butter is followed by a size, so the
    product is butter. In "Butter croissant 60 g" it is followed by another
    word, so the product is a croissant.
    """
    words = _name_words[product_id]
    positions = [index for index, word in enumerate(words) if word in wanted]
    if not wanted or len({words[index] for index in positions}) < len(wanted):
        return False
    following = words[positions[-1] + 1] if positions[-1] + 1 < len(words) else ""
    return following == "" or bool(_SIZE_WORD.match(following))


def vocabulary() -> dict[str, list[dict]]:
    """The claims and labels that can be used as search filters, most common first."""
    counts = {"claims": Counter(), "labels": Counter()}
    for product in _products.values():
        counts["claims"].update(product.claims)
        counts["labels"].update(product.labels)
    return {
        kind: [
            {"id": key, "meaning": key.replace("_", " ").lower(), "products": count}
            for key, count in counter.most_common()
            if count >= MIN_PRODUCTS_FOR_VOCABULARY
        ]
        for kind, counter in counts.items()
    }
