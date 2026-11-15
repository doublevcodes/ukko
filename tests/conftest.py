"""Shared test setup: a throwaway data folder and fakes for Valio, the LLM and Exa.

The environment is set before any project module is imported, so nothing in
the tests can read the real .env keys, write to the real data folder, or
reach the live Valio API.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
_tmp = Path(tempfile.mkdtemp(prefix="ukko-tests-"))
(_tmp / "data").mkdir()
shutil.copytree(ROOT / "config", _tmp / "config")
os.environ.update({
    "UKKO_DATA_DIR": str(_tmp / "data"), "UKKO_CONFIG_DIR": str(_tmp / "config"),
    "VALIO_BASE_URL": "https://valio.invalid", "VALIO_API_KEY": "test-key",
    "GEMINI_API_KEY": "", "EXA_API_KEY": "", "ENFORCE_ORDER_CUTOFF": "true",
    "CAMERA_FEED_URL": "",  # no camera: nothing in the tests opens a video stream
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import authority, config, gateway_client, llm, payments, pipeline, state  # noqa: E402
from app import settings as app_settings  # noqa: E402
from app.models import PolicyConfig  # noqa: E402
from app.web import exa_checks  # noqa: E402
from gateway import catalogue, keys, ledger, merchant, offer_store, supplier_log, valio_client  # noqa: E402
from gateway import settings as gateway_settings  # noqa: E402
from gateway.main import app as gateway_app  # noqa: E402
from tests import fakes  # noqa: E402

SHELF = """
defaults:
  reorder_quantity: 100
  unit: l
  usual_quantity: 100
  lead_time_days: 3
  constraints: []
  preferences: []
  search_terms: ""
products:
  milk_lactose_free:
    name: "Lactose-free semi-skimmed milk 1 L"
    product_id: "1001"
    reorder_quantity: 100
    unit: l
    usual_quantity: 100
    constraints: [FREE_FROM_LACTOSE]
    lead_time_days: 3
    search_terms: "semi skimmed milk"
"""

CAMERA_FOODS = """
foods:
  milk_lactose_free:
    name: "Lactose-free semi-skimmed milk 1 L"
    product_id: "1001"
    search_terms: "semi skimmed milk"
    constraints: [FREE_FROM_LACTOSE]
  mystery_soda:
    name: "Mystery soda"
    search_terms: "soda soft drink"
"""


def _fresh_files() -> None:
    """Empty the data folder and write the fixture catalogue and shelf."""
    shutil.rmtree(gateway_settings.GATEWAY_DATA_DIR, ignore_errors=True)
    for name in (
        "audit_log.jsonl", "catalogue_index.json", "usage_cycles.json",
        "stripe_customer.json", "live_state.json",
    ):
        (app_settings.DATA_DIR / name).unlink(missing_ok=True)
    (app_settings.DATA_DIR / "products.json").write_text(fakes.catalogue_file_content(), encoding="utf-8")
    app_settings.SHELF_FILE.write_text(SHELF, encoding="utf-8")
    app_settings.CAMERA_FOODS_FILE.write_text(CAMERA_FOODS, encoding="utf-8")
    config.save_policy(PolicyConfig())


def _seed_kitchen_card() -> None:
    """Every order charges Stripe; tests start with a Visa ····4242 already on file."""
    payments._save_profile("cus_test_1", "pm_test_visa", "Visa", "4242")


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A clean gateway and a clean Ukko wired together in memory, with fakes outside."""
    _fresh_files()
    valio, fake_llm, exa = fakes.FakeValio(), fakes.FakeLLM(), fakes.FakeExa()
    stripe = fakes.FakeStripe()
    valio_client.use_transport(valio.transport())
    monkeypatch.setattr(catalogue, "MIN_PRODUCTS_FOR_VOCABULARY", 1)
    catalogue.load()
    ledger.load(), merchant.reset(), supplier_log.reset(), keys.reset(), offer_store.reset()

    gateway_client.use_client(TestClient(gateway_app))
    monkeypatch.setattr(llm, "complete_json", fake_llm.complete_json)
    monkeypatch.setattr(llm, "complete_text", fake_llm.complete_text)
    monkeypatch.setattr(exa_checks, "_exa_search", exa.search)
    payments.use_backend(stripe)
    _seed_kitchen_card()
    exa_checks._cache.clear()
    pipeline._vocabulary_cache = None
    authority.forget()
    state.load()
    return SimpleNamespace(valio=valio, llm=fake_llm, exa=exa, stripe=stripe, orders=ledger.all_orders)
