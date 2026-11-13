"""Live web checks through Exa: recalls, substitute suitability, price sanity.

Each check searches the web, then asks the language model to judge using only
the text that came back. Web text is untrusted: it is quoted as data and the
model is told to ignore any instructions inside it. A check that cannot run
returns severity "check_failed"; it is never skipped silently.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as TookTooLong
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from exa_py import Exa

from app import llm, settings
from app.models import Source, WebFinding
from app.web import judge

EXA_TIMEOUT_SECONDS = 8
RECALL_WINDOW_DAYS = 90
CACHE_SECONDS = 600
RESULTS_PER_SEARCH = 5
_cache: dict[tuple, tuple[float, WebFinding]] = {}
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="exa")
log = logging.getLogger("stockpot.web")


def _exa_search(query: str, since: date | None) -> list[dict[str, Any]]:
    """One Exa search with highlights. Tests replace this function."""
    if not settings.EXA_API_KEY:
        raise RuntimeError("EXA_API_KEY is not set in .env")
    options: dict[str, Any] = {"contents": {"highlights": True}, "num_results": RESULTS_PER_SEARCH}
    if since:
        options["start_published_date"] = since.isoformat()
    response = Exa(api_key=settings.EXA_API_KEY, timeout=EXA_TIMEOUT_SECONDS).search(query, **options)
    return [{
        "title": result.title or result.url, "url": result.url, "published_date": result.published_date,
        "text": " ".join(result.highlights or []) or (result.text or ""),
    } for result in response.results]


def _search(query: str, since: date | None = None) -> list[dict[str, Any]]:
    """Search with a hard 8-second limit, whatever the SDK does."""
    return _pool.submit(_exa_search, query, since).result(timeout=EXA_TIMEOUT_SECONDS)


def _run(check_type: str, product_id: str | None, query: str, question: str, wording: judge.Wording,
         since: date | None = None) -> WebFinding:
    """Search, judge, and turn any failure into a visible `check_failed` finding."""
    def failed(why: str) -> WebFinding:
        return WebFinding(check_type=check_type, query=query, severity="check_failed", summary=why, product_id=product_id)

    try:
        results = _search(query, since)
    except TookTooLong:
        return failed(f"The web search took longer than {EXA_TIMEOUT_SECONDS} seconds.")
    except Exception as error:  # noqa: BLE001  any Exa problem must surface as a failed check
        log.warning("Exa search failed for %r: %s: %s", query, type(error).__name__, str(error)[:300])
        if not settings.EXA_API_KEY:
            return failed("The web search could not run: EXA_API_KEY is not set in .env.")
        return failed("The web search could not run: the search service returned an error.")
    if not results:
        severity = "caution" if wording.nothing_found_is_a_concern else "none"
        return WebFinding(check_type=check_type, query=query, severity=severity, summary=wording.nothing_found, product_id=product_id)
    try:
        severity, summary, cited = judge.judge(question, results, wording)
    except llm.LLMError as error:
        return failed(f"The search ran, but its results could not be assessed. {error}")
    sources = [Source(title=results[i]["title"][:200], url=results[i]["url"], published_date=results[i]["published_date"],
                      snippet=results[i]["text"][:200]) for i in cited]
    return WebFinding(check_type=check_type, query=query, severity=severity, summary=summary, sources=sources, product_id=product_id)


def _cached(key: tuple, make: Any) -> WebFinding:
    """Reuse a result for 10 minutes. Failed checks are not kept, so they are retried."""
    saved = _cache.get(key)
    if saved and time.time() - saved[0] < CACHE_SECONDS:
        return saved[1]
    finding = make()
    if finding.severity != "check_failed":
        _cache[key] = (time.time(), finding)
    return finding


def _full_name(product_name: str, brand: str | None) -> str:
    brand = (brand or "").strip()
    return product_name if not brand or brand.lower() in product_name.lower() else f"{brand} {product_name}"


def recall_check(product_name: str, brand: str | None, product_id: str | None = None) -> WebFinding:
    """Look for recalls, food-safety notices or supply problems from the last 90 days."""
    name = _full_name(product_name, brand)
    query = f"Product recall, food safety notice or supply disruption: {name}"
    today = date.today()
    since = today - timedelta(days=RECALL_WINDOW_DAYS)
    # The dates are spelled out so that an old notice on a recently updated page is left out.
    question = (f"Today is {today:%d %B %Y}. Is there a recall, food-safety notice or supply disruption issued on or "
                f"after {since:%d %B %Y} (the last {RECALL_WINDOW_DAYS} days) that affects the product '{name}'? "
                "Leave out anything older than that.")
    wording = judge.Wording(
        nothing_found=f"No recent recall notices found for {name}.",
        serious="an official recall or safety notice names this exact product",
        concern="a recall or disruption affects the same brand or a very similar product",
        clear="",
    )
    return _cached(("recall", product_id or name), lambda: _run("recall", product_id, query, question, wording, since))


def substitute_check(substitute_product: str, constraint: str, product_id: str | None = None) -> WebFinding:
    """Does the substitute really meet the requirement, according to the producer or an authority?"""
    meaning = constraint.replace("_", " ").lower()
    query = f"{substitute_product}: is it {meaning}? Producer or official product information"
    question = f"According to the producer or an authority, is the product '{substitute_product}' {meaning}?"
    wording = judge.Wording(
        nothing_found=f"No source was found to confirm that {substitute_product} is {meaning}.",
        nothing_found_is_a_concern=True,
        serious=f"a source says the product is NOT {meaning}",
        concern="the sources are unclear, conflicting, or about a different product",
        clear=f"the producer or an authority confirms the product is {meaning}",
    )
    return _cached(("substitute", product_id or substitute_product, constraint),
                   lambda: _run("substitute_suitability", product_id, query, question, wording))


def price_check(product_name: str, price: Decimal, per: str, product_id: str | None = None) -> WebFinding:
    """Is the quoted price unusually high compared with prices seen on the web?"""
    query = f"{product_name} price per {per} in Finland"
    question = (f"The supplier quotes {price:.2f} EUR per {per} (excluding VAT) for '{product_name}'. "
                "Compared with prices in the sources, is that unusually high?")
    wording = judge.Wording(
        nothing_found=f"No comparable public prices were found for {product_name}.",
        serious="", concern="the quote is clearly higher (roughly 30% or more) than comparable prices in the sources",
        clear="the quote is in line with, or below, comparable prices in the sources",
    )
    return _cached(("price", product_id or product_name, str(price)),
                   lambda: _run("price_sanity", product_id, query, question, wording))
