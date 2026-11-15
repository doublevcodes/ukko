"""Stripe test-mode payments: save a kitchen card once, charge it on every order.

AP2 still authorises the cart. Stripe moves the (test) money. Card details are
entered in an embedded Checkout (setup mode) inside the dashboard; later orders
use PaymentIntent off-session with the saved PaymentMethod.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Protocol

import stripe

from app import settings

PROFILE_VERSION = 1


class PaymentError(Exception):
    """A charge or setup step failed. The message is plain English for the log."""


class Backend(Protocol):
    def configured(self) -> bool: ...
    def create_customer(self, name: str) -> str: ...
    def create_embedded_setup(self, customer_id: str, return_url: str) -> dict[str, str]: ...
    def setup_payment_method(self, session_id: str) -> tuple[str, str, dict[str, Any]]: ...
    def set_default_payment_method(self, customer_id: str, payment_method_id: str) -> None: ...
    def charge(self, customer_id: str, payment_method_id: str, amount_cents: int,
               currency: str, request_id: str, description: str) -> str: ...


class StripeBackend:
    """Live Stripe API (test mode when STRIPE_SECRET_KEY is sk_test_…)."""

    def __init__(self, secret_key: str):
        self._key = secret_key

    def configured(self) -> bool:
        return bool(self._key)

    def _api(self) -> None:
        stripe.api_key = self._key

    def create_customer(self, name: str) -> str:
        self._api()
        return stripe.Customer.create(name=name, metadata={"buyer_id": settings.BUYER_ID}).id

    def create_embedded_setup(self, customer_id: str, return_url: str) -> dict[str, str]:
        self._api()
        # Omit payment_method_types so Stripe can pick methods from the Dashboard.
        # redirect_on_completion=if_required keeps card setup inside the dialog.
        session = stripe.checkout.Session.create(
            mode="setup",
            ui_mode="embedded_page",
            customer=customer_id,
            currency="eur",
            return_url=return_url,
            redirect_on_completion="if_required",
        )
        if not session.client_secret:
            raise PaymentError("Stripe did not return a client secret for the setup form.")
        return {"client_secret": session.client_secret, "session_id": session.id}

    def setup_payment_method(self, session_id: str) -> tuple[str, str, dict[str, Any]]:
        self._api()
        try:
            session = stripe.checkout.Session.retrieve(session_id, expand=["setup_intent"])
        except stripe.error.InvalidRequestError as error:
            raise PaymentError(f"Stripe could not find that Checkout session ({error.user_message or error.code}).") from error
        if session.mode != "setup" or session.status != "complete":
            raise PaymentError("Card setup was not completed. Open Add card and try again.")
        setup = session.setup_intent
        if isinstance(setup, str) or setup is None:
            raise PaymentError("Stripe did not return a setup for this session.")
        payment_method_id = setup.payment_method if isinstance(setup.payment_method, str) else setup.payment_method.id
        customer_id = session.customer if isinstance(session.customer, str) else session.customer.id
        method = stripe.PaymentMethod.retrieve(payment_method_id)
        # Stripe SDK objects are not plain dicts — read attributes, not .get().
        card = method.card
        brand = (getattr(card, "brand", None) or "card")
        last4 = getattr(card, "last4", None) or "????"
        return customer_id, payment_method_id, {"brand": str(brand).title(), "last4": str(last4)}

    def set_default_payment_method(self, customer_id: str, payment_method_id: str) -> None:
        self._api()
        stripe.Customer.modify(customer_id, invoice_settings={"default_payment_method": payment_method_id})

    def charge(self, customer_id: str, payment_method_id: str, amount_cents: int,
               currency: str, request_id: str, description: str) -> str:
        self._api()
        try:
            intent = stripe.PaymentIntent.create(
                amount=amount_cents,
                currency=currency.lower(),
                customer=customer_id,
                payment_method=payment_method_id,
                off_session=True,
                confirm=True,
                description=description,
                metadata={"request_id": request_id, "buyer_id": settings.BUYER_ID},
            )
        except stripe.error.CardError as error:
            raise PaymentError(f"The kitchen card was declined ({error.user_message or error.code}). "
                               "Open Change card in Settings if you need to use another card.") from error
        except stripe.error.InvalidRequestError as error:
            raise PaymentError(f"Stripe could not charge the saved card ({error.user_message or error.code}). "
                               "Open Change card in Settings and complete setup again.") from error
        except stripe.error.StripeError as error:
            raise PaymentError(f"Stripe could not take payment ({error.user_message or type(error).__name__}).") from error
        if intent.status != "succeeded":
            raise PaymentError(f"Stripe payment ended as '{intent.status}', not succeeded. Nothing was ordered.")
        return intent.id


_backend: Backend | None = None


def use_backend(backend: Backend | None) -> None:
    """Tests swap in a fake; None restores the live Stripe backend from settings."""
    global _backend
    _backend = backend


def _live() -> Backend:
    global _backend
    if _backend is None:
        _backend = StripeBackend(settings.STRIPE_SECRET_KEY)
    return _backend


def _profile_path() -> Path:
    return settings.STRIPE_CUSTOMER_FILE


def _read_file() -> dict[str, Any] | None:
    path = _profile_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if data.get("customer_id") else None


def _ready_profile() -> dict[str, Any] | None:
    data = _read_file()
    if not data or not data.get("payment_method_id"):
        return None
    return data


def _save_profile(customer_id: str, payment_method_id: str, brand: str, last4: str) -> None:
    from app import durable

    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": PROFILE_VERSION,
        "customer_id": customer_id,
        "payment_method_id": payment_method_id,
        "brand": brand,
        "last4": last4,
    }
    path = _profile_path()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    durable.mirror(path)


def clear_profile() -> None:
    """Forget the saved card on disk (tests)."""
    _profile_path().unlink(missing_ok=True)


def status() -> dict[str, Any]:
    """What the dashboard shows about the kitchen card."""
    backend = _live()
    profile = _ready_profile()
    publishable = settings.STRIPE_PUBLISHABLE_KEY
    return {
        "configured": backend.configured() and bool(publishable),
        "secret_configured": backend.configured(),
        "publishable_configured": bool(publishable),
        "publishable_key": publishable if publishable else None,
        "ready": bool(profile) and backend.configured(),
        "brand": (profile or {}).get("brand") or None,
        "last4": (profile or {}).get("last4") or None,
        "label": (
            f"{profile['brand']} ····{profile['last4']}" if profile
            else ("Add a kitchen card (once)" if backend.configured() and publishable
                  else "Stripe not configured")
        ),
    }


def _ensure_customer() -> str:
    stored = _read_file()
    if stored:
        return stored["customer_id"]
    customer_id = _live().create_customer("Ukko demo kitchen")
    _save_profile(customer_id, "", "", "")
    return customer_id


def create_setup_session() -> dict[str, str]:
    """Start embedded Checkout in setup mode. Returns client_secret + session_id."""
    backend = _live()
    if not backend.configured():
        raise PaymentError("Stripe is not configured. Set STRIPE_SECRET_KEY (test mode) in .env.")
    if not settings.STRIPE_PUBLISHABLE_KEY:
        raise PaymentError("Stripe publishable key is missing. Set STRIPE_PUBLISHABLE_KEY in .env.")
    customer_id = _ensure_customer()
    base = settings.public_base_url()
    return backend.create_embedded_setup(
        customer_id,
        return_url=f"{base}/payments/return?session_id={{CHECKOUT_SESSION_ID}}",
    )


def finish_setup(session_id: str) -> dict[str, Any]:
    """After embedded Checkout completes, store the PaymentMethod as the kitchen card."""
    backend = _live()
    if not backend.configured():
        raise PaymentError("Stripe is not configured.")
    customer_id, payment_method_id, card = backend.setup_payment_method(session_id)
    backend.set_default_payment_method(customer_id, payment_method_id)
    _save_profile(customer_id, payment_method_id, card["brand"], card["last4"])
    return status()


def _cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def charge(amount: Decimal, currency: str, request_id: str, description: str) -> tuple[str, str]:
    """Charge the saved kitchen card. Returns (payment_intent_id, payment_method_label)."""
    backend = _live()
    if not backend.configured():
        raise PaymentError("Stripe is not configured. Set STRIPE_SECRET_KEY (test mode) in .env.")
    profile = _ready_profile()
    if not profile:
        raise PaymentError("No kitchen card on file. Open Settings → Payment, add a card once, then try again.")
    payment_intent_id = backend.charge(
        profile["customer_id"], profile["payment_method_id"], _cents(amount),
        currency, request_id, description,
    )
    label = f"Stripe test · {profile.get('brand') or 'Card'} ····{profile.get('last4') or '????'}"
    return payment_intent_id, label
