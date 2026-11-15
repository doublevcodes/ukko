"""Stripe test-mode payments: save a kitchen card once, charge it on every order.

AP2 still authorises the cart. Stripe moves the (test) money. Card details are
entered in an embedded Checkout (setup mode) inside the dashboard; later orders
use PaymentIntent off-session with the saved PaymentMethod.
"""

from __future__ import annotations

import json
import time
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
        try:
            return stripe.Customer.create(name=name, metadata={"buyer_id": settings.BUYER_ID}).id
        except stripe.error.StripeError as error:
            raise PaymentError(f"Stripe could not create a customer ({error.user_message or type(error).__name__}).") from error

    def create_embedded_setup(self, customer_id: str, return_url: str) -> dict[str, str]:
        self._api()
        # Omit payment_method_types so Stripe can pick methods from the Dashboard.
        # redirect_on_completion=if_required keeps card setup inside the dialog.
        try:
            session = stripe.checkout.Session.create(
                mode="setup",
                ui_mode="embedded_page",
                customer=customer_id,
                currency="eur",
                return_url=return_url,
                redirect_on_completion="if_required",
            )
        except stripe.error.InvalidRequestError as error:
            # Stale customer id in the saved profile (common after switching Stripe accounts
            # or restoring Blob state onto a different key) — signal the caller to reset.
            message = (error.user_message or str(error) or "").lower()
            if "no such customer" in message or error.code in {"resource_missing"}:
                raise PaymentError("stale_customer") from error
            raise PaymentError(f"Stripe could not start card setup ({error.user_message or error.code}).") from error
        except stripe.error.StripeError as error:
            raise PaymentError(f"Stripe could not start card setup ({error.user_message or type(error).__name__}).") from error
        if not session.client_secret:
            raise PaymentError("Stripe did not return a client secret for the setup form.")
        return {"client_secret": session.client_secret, "session_id": session.id}

    def setup_payment_method(self, session_id: str) -> tuple[str, str, dict[str, Any]]:
        self._api()
        try:
            session = stripe.checkout.Session.retrieve(
                session_id, expand=["setup_intent", "setup_intent.payment_method"],
            )
        except stripe.error.InvalidRequestError as error:
            raise PaymentError(f"Stripe could not find that Checkout session ({error.user_message or error.code}).") from error
        except stripe.error.StripeError as error:
            raise PaymentError(f"Stripe could not load the card setup ({error.user_message or type(error).__name__}).") from error
        if session.mode != "setup":
            raise PaymentError("Card setup was not completed. Open Add card and try again.")
        if session.status != "complete":
            # Caller may retry briefly: embedded Checkout onComplete can fire before status flips.
            raise PaymentError("Card setup was not completed. Open Add card and try again.")
        setup = session.setup_intent
        if isinstance(setup, str) or setup is None:
            raise PaymentError("Stripe did not return a setup for this session.")
        raw_pm = setup.payment_method
        if raw_pm is None:
            raise PaymentError("Stripe did not return a payment method for this session.")
        payment_method_id = raw_pm if isinstance(raw_pm, str) else raw_pm.id
        if session.customer is None:
            raise PaymentError("Stripe did not return a customer for this session.")
        customer_id = session.customer if isinstance(session.customer, str) else session.customer.id
        try:
            method = raw_pm if not isinstance(raw_pm, str) else stripe.PaymentMethod.retrieve(payment_method_id)
        except stripe.error.StripeError as error:
            raise PaymentError(f"Stripe could not read the saved card ({error.user_message or type(error).__name__}).") from error
        # Stripe SDK Card objects are not dicts — never call .get() on them.
        card = getattr(method, "card", None)
        if card is not None and hasattr(card, "to_dict"):
            card_data = card.to_dict()
            brand = card_data.get("brand") or "card"
            last4 = card_data.get("last4") or "????"
        else:
            brand = getattr(card, "brand", None) or "card"
            last4 = getattr(card, "last4", None) or "????"
        return customer_id, payment_method_id, {"brand": str(brand).title(), "last4": str(last4)}

    def set_default_payment_method(self, customer_id: str, payment_method_id: str) -> None:
        self._api()
        try:
            stripe.Customer.modify(customer_id, invoice_settings={"default_payment_method": payment_method_id})
        except stripe.error.StripeError as error:
            raise PaymentError(f"Stripe could not save the card as default ({error.user_message or type(error).__name__}).") from error

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
    if stored and stored.get("customer_id"):
        return stored["customer_id"]
    customer_id = _live().create_customer("Ukko demo kitchen")
    _save_profile(customer_id, "", "", "")
    return customer_id


def _reset_customer() -> str:
    """Forget a stale Stripe customer id and create a fresh one for this secret key."""
    clear_profile()
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
    base = settings.public_base_url()
    return_url = f"{base}/payments/return?session_id={{CHECKOUT_SESSION_ID}}"
    customer_id = _ensure_customer()
    try:
        return backend.create_embedded_setup(customer_id, return_url=return_url)
    except PaymentError as error:
        if str(error) != "stale_customer":
            raise
        customer_id = _reset_customer()
        return backend.create_embedded_setup(customer_id, return_url=return_url)


def finish_setup(session_id: str) -> dict[str, Any]:
    """After embedded Checkout completes, store the PaymentMethod as the kitchen card."""
    backend = _live()
    if not backend.configured():
        raise PaymentError("Stripe is not configured.")
    # Embedded Checkout's onComplete can fire a moment before session.status is complete.
    last_error: PaymentError | None = None
    for attempt in range(6):
        try:
            customer_id, payment_method_id, card = backend.setup_payment_method(session_id)
            break
        except PaymentError as error:
            last_error = error
            if "not completed" not in str(error).lower() or attempt == 5:
                raise
            time.sleep(0.4)
    else:
        raise last_error or PaymentError("Card setup was not completed. Open Add card and try again.")
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
