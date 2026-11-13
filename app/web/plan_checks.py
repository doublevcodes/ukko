"""Pipeline step 4: which web checks to run for a plan, run side by side."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from app import offer_rules
from app.models import OrderRequest, Plan, WebFinding
from app.web import exa_checks

MAX_CONSTRAINTS_CHECKED = 2  # per substitute, to keep the demo fast and save credits
PER = {"l": "litre", "kg": "kilo", "pcs": "pack"}
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="webcheck")


def _jobs(request: OrderRequest, plan: Plan) -> list[tuple[str, str | None, Callable[[], WebFinding]]]:
    """(check type, product id, the call to make) for every check this plan needs."""
    jobs: list[tuple[str, str | None, Callable[[], WebFinding]]] = []
    for line in plan.lines:
        offer = line.offer
        jobs.append(("recall", offer.product_id,
                     lambda o=offer: exa_checks.recall_check(o.name, o.brand, o.product_id)))
        if line.is_substitute:
            for need in request.constraints[:MAX_CONSTRAINTS_CHECKED]:
                jobs.append(("substitute_suitability", offer.product_id,
                             lambda o=offer, n=need: exa_checks.substitute_check(o.name, n, o.product_id)))
    main = plan.lines[0].offer
    price = offer_rules.price_per_unit(main, request)
    if price is not None:
        jobs.append(("price_sanity", main.product_id,
                     lambda: exa_checks.price_check(main.name, price, PER[request.unit], main.product_id)))
    return jobs


def for_plan(request: OrderRequest, plan: Plan) -> list[WebFinding]:
    """Recall check per product, suitability check per substitute, one price check.

    Never raises: a check that breaks comes back as `check_failed`.
    """
    jobs = _jobs(request, plan)
    futures = [(check_type, product_id, _pool.submit(call)) for check_type, product_id, call in jobs]
    findings = []
    for check_type, product_id, future in futures:
        try:
            findings.append(future.result())
        except Exception as error:  # noqa: BLE001  a broken check must still show up, as failed
            findings.append(WebFinding(
                check_type=check_type, query="", severity="check_failed", product_id=product_id,
                summary=f"The check could not run because of an unexpected problem ({type(error).__name__}).",
            ))
    return findings
