"""Ukko buying agent: HTTP routes.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
Dashboard: http://localhost:8000   API description: http://localhost:8000/docs
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import authority, camera, categories, config, intake, payments, settings, state, trust_tests, vision
from app.gateway_client import GatewayError, GatewayRefused
from app.models import PolicyConfig, SensorEvent
from app.payments import PaymentError

STATIC = Path(__file__).parent / "static"


class TypedRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    reply_to: str | None = Field(default=None, description="Id of a request whose question this text answers.")


class ApprovalAction(BaseModel):
    action: Literal["approve", "reject"]
    note: str = Field(default="", max_length=300)


class ShelfReorderSettings(BaseModel):
    """Per-item reorder controls editable from the inventory table."""

    reorder_below: Decimal = Field(ge=0, description="Order when the camera sees this many units or fewer.")
    reorder_quantity: Decimal = Field(gt=0, description="How many units to order when a reorder is triggered.")


def _sign_standing_limits(reason: str) -> None:
    """Have the manager key sign the agent's limits (AP2). If the supplier is down, it happens at the first order."""
    try:
        limits = authority.renew(state.policy)
    except (GatewayError, GatewayRefused) as error:
        authority.forget()
        state.log(None, "authority", f"{reason} The agent's standing limits could not be signed yet: {error}")
        return
    state.log(None, "authority", f"{reason} The manager key signed the agent's standing limits (AP2): up to "
              f"{limits.max_order_eur:.2f} EUR per order and {limits.budget_eur:.2f} EUR in total, at {limits.supplier_id} only.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.load()
    _sign_standing_limits("Ukko started.")
    intake.wire_vision_reorders()
    camera.start()
    vision.start()
    watcher = asyncio.create_task(_watch_forecasts())
    yield
    watcher.cancel()


async def _watch_forecasts() -> None:
    """Every few minutes, order ahead for anything forecast to run out within 7 days."""
    while True:
        for record in await asyncio.to_thread(intake.check_forecasts):
            asyncio.get_running_loop().run_in_executor(None, intake.run_sensor, record)
        await asyncio.sleep(settings.FORECAST_CHECK_MINUTES * 60)


class FreshStaticFiles(StaticFiles):
    """Static files the browser must re-check before reusing its copy.

    Without this a reload can pair a new index.html with an older cached script,
    and the dashboard breaks until someone forces a full refresh.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="Ukko buying agent", version="0.1.0", lifespan=lifespan)
# The sensor device and a phone on the local network both call this API.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", FreshStaticFiles(directory=STATIC), name="static")


@app.get("/health")
def health() -> dict[str, Any]:
    pay = payments.status()
    return {"status": "ok", "service": "ukko", "gateway_url": settings.GATEWAY_URL,
            "gemini_key_set": bool(settings.GEMINI_API_KEY), "exa_key_set": bool(settings.EXA_API_KEY),
            "stripe_configured": pay["configured"], "stripe_ready": pay["ready"],
            "camera_connected": camera.connected()}


@app.post("/reset")
def reset_runtime() -> dict[str, Any]:
    """Clear requests, shelf live fields, audit and on-order; keep Stripe card and keys."""
    state.reset_runtime()
    return {"ok": True, "requests": 0, "message": "Runtime state cleared."}


@app.get("/camera/feed", include_in_schema=False)
def camera_feed() -> StreamingResponse:
    """The shelf camera's video for the dashboard, relayed from Ukko's one connection to the camera."""
    # Allow a remount during a short stall: we still have the last picture even when
    # connected() is false (no fresh frame for a few seconds).
    if not camera.has_frame():
        raise HTTPException(503, "The camera is not sending video.")
    return StreamingResponse(camera.relay(), media_type=f"multipart/x-mixed-replace; boundary={camera.BOUNDARY}",
                             headers={"Cache-Control": "no-store"})


@app.post("/events/sensor")
def sensor(event: SensorEvent, tasks: BackgroundTasks) -> dict[str, Any]:
    """A shelf level from the camera device. Only the first "low" or "empty" starts a reorder."""
    result, record = intake.sensor_event(event)
    if record:
        tasks.add_task(intake.run_sensor, record)
    return {"result": result, "request_id": record.id if record else None}


@app.post("/requests")
def typed_request(body: TypedRequest, tasks: BackgroundTasks) -> dict[str, Any]:
    record = intake.new_typed_request(body.text.strip(), body.reply_to)
    tasks.add_task(intake.run_typed, record)
    return {"request_id": record.id}


def _enrich_shelf_for_dashboard(snapshot: dict[str, Any]) -> None:
    """Attach live reorder-model fields each shelf row needs for the inventory table."""
    from app import forecast
    model = config.load_reorder_model()
    snapshot["reorder_model"] = {
        "model_day_seconds": model.model_day_seconds,
        "alpha": model.alpha,
        "sigma_factor": model.sigma_factor,
        "z": model.z,
        "default_cover_days": model.default_cover_days,
        "default_lead_days": model.default_lead_days,
        "multiplier_min": model.multiplier_min,
        "multiplier_max": model.multiplier_max,
        "extra_lead_days_min": model.extra_lead_days_min,
        "extra_lead_days_max": model.extra_lead_days_max,
        "demo_clock": model.model_day_seconds != 86400,
        "demo_clock_label": (
            f"Demo clock: 1 day = {max(1, int(round(model.model_day_seconds / 60)))} min"
            if model.model_day_seconds != 86400 else None
        ),
    }
    now = state.now()
    for row in snapshot["shelf"]:
        key = row["product_key"]
        live = state.shelf.get(key)
        if live is None:
            continue
        forecast.seed_usage(live, now)
        view = forecast.evaluate_row(key, live, now=now, visible=live.quantity)
        row.update({
            "usage_per_day": view["usage_per_day"],
            "usage_label": view["usage_label"],
            "usage_observations": view["usage_observations"],
            "days_of_cover": view["days_of_cover"],
            "reorder_point": view["reorder_point"],
            "on_hand_units": view["on_hand_units"],
            "on_order_units": view["on_order_units"],
            "inventory_position": view["inventory_position"],
            "needs_reorder": view["needs_reorder"],
            "model_order_quantity": view["order_quantity"],
            "delivery_due": view["delivery_due"],
            "units_per_visible_item": view["units_per_visible_item"],
            "reorder_why": live.reorder_why or view["why"],
            "quantity_why": view["quantity_why"],
        })


@app.get("/state")
def get_state() -> dict[str, Any]:
    """Everything the dashboard shows, in one call."""
    vision.note_viewer()
    # After a Vercel cold start the standing limits may still be missing if buyer
    # registration raced with Blob restore. Retry here so the dashboard heals.
    if authority.summary() is None:
        _sign_standing_limits("Reconnecting to the supplier.")
    snapshot = state.snapshot()
    for view in snapshot["requests"]:
        token = state.requests[view["id"]].approval_token if view["id"] in state.requests else None
        if token:
            view["approval"] = {"url": f"{settings.public_base_url()}/approve/{token}"}
    snapshot["standing_limits"] = authority.summary()
    snapshot["category_names"] = categories.all_names()
    snapshot["payment"] = payments.status()
    snapshot["health"] = health()
    _enrich_shelf_for_dashboard(snapshot)
    return snapshot


@app.post("/payments/setup")
def start_payment_setup() -> dict[str, Any]:
    """Start embedded Stripe Checkout (setup mode) for the kitchen card form."""
    try:
        session = payments.create_setup_session()
        return {
            "client_secret": session["client_secret"],
            "session_id": session["session_id"],
            "publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
        }
    except PaymentError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:  # noqa: BLE001  never return a blank 500 to the card dialog
        raise HTTPException(400, f"Card setup failed: {type(error).__name__}: {error}") from error


class PaymentComplete(BaseModel):
    session_id: str = Field(min_length=1)


@app.post("/payments/complete")
def complete_payment_setup(body: PaymentComplete) -> dict[str, Any]:
    """Save the card after the embedded Checkout finishes (no redirect)."""
    try:
        return {"payment": payments.finish_setup(body.session_id)}
    except PaymentError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:  # noqa: BLE001  never return a blank 500 to the card dialog
        raise HTTPException(400, f"Could not save the card: {type(error).__name__}: {error}") from error


@app.get("/payments/return", include_in_schema=False)
def payment_setup_return(session_id: str = "") -> RedirectResponse:
    """Fallback when a payment method redirects away (for example 3-D Secure)."""
    if not session_id:
        return RedirectResponse("/?payment=missing", status_code=303)
    try:
        payments.finish_setup(session_id)
    except PaymentError:
        return RedirectResponse("/?payment=failed", status_code=303)
    return RedirectResponse("/?payment=ready", status_code=303)


def _decide(record: state.RequestRecord | None, body: ApprovalAction, tasks: BackgroundTasks, where: str) -> dict[str, Any]:
    if record is None:
        raise HTTPException(404, "No such request, or this approval link has already been used.")
    if not intake.claim_approval(record):
        raise HTTPException(409, "This request is not waiting for approval.")
    # Only an explicit reject cancels; anything else must not fall through to reject.
    if body.action == "reject":
        intake.reject(record, body.note, where)
    elif body.action == "approve":
        tasks.add_task(intake.run_approved, record, body.note, where)
    else:
        raise HTTPException(400, f"Unknown approval action '{body.action}'.")
    return {"request_id": record.id, "action": body.action}


@app.post("/approvals/{request_id}")
def approve_from_dashboard(request_id: str, body: ApprovalAction, tasks: BackgroundTasks) -> dict[str, Any]:
    return _decide(state.requests.get(request_id), body, tasks, "dashboard")


@app.get("/approve/{token}", include_in_schema=False)
def phone_page(token: str) -> FileResponse:
    return FileResponse(STATIC / "approve.html")


@app.get("/phone/{token}")
def phone_view(token: str) -> dict[str, Any]:
    """What the phone page shows. The code in the link works once."""
    record = state.by_token(token)
    if record is None:
        raise HTTPException(404, "This approval link has already been used or has expired.")
    return state.request_view(record)


@app.post("/phone/{token}")
def approve_from_phone(token: str, body: ApprovalAction, tasks: BackgroundTasks) -> dict[str, Any]:
    return _decide(state.by_token(token), body, tasks, "phone")


class TrustTestRequest(BaseModel):
    scenario: Literal["price_changed", "expired", "unknown_signer", "reused", "wrong_buyer"]


@app.post("/trust-tests")
def run_trust_test(body: TrustTestRequest) -> dict[str, Any]:
    """Break a real signed offer in one way and run the same verifier the pipeline uses."""
    try:
        return trust_tests.run(body.scenario)
    except (GatewayError, GatewayRefused, ValueError, RuntimeError) as error:
        raise HTTPException(502, str(error)) from error


@app.get("/policy")
def get_policy() -> dict[str, Any]:
    return {"policy": state.policy.model_dump(mode="json"), "category_names": categories.all_names()}


@app.put("/policy")
def save_policy(policy: PolicyConfig) -> dict[str, Any]:
    """Change the rules. The file is rewritten and the agent's standing limits are signed again."""
    unknown = [name for name in policy.allowed_categories if name not in categories.all_names()]
    if unknown:
        raise HTTPException(422, f"Unknown product group: {', '.join(unknown)}. See config/categories.yaml.")
    before = state.policy.model_dump(mode="json")
    config.save_policy(policy)
    state.policy = policy
    after = policy.model_dump(mode="json")
    changed = [f"{key}: {before[key]} -> {after[key]}" for key in after if before[key] != after[key]]
    _sign_standing_limits(f"Rules changed in the dashboard ({'; '.join(changed) or 'no values changed'}).")
    return {"policy": after}


@app.put("/shelf/{product_key}")
def save_shelf_reorder(product_key: str, body: ShelfReorderSettings) -> dict[str, Any]:
    """Set when this inventory item reorders and how many units to order. Saved to camera_foods.yaml."""
    with state.lock:
        row = state.shelf.get(product_key)
        if row is None:
            raise HTTPException(404, f"Unknown inventory item '{product_key}'.")
        try:
            config.update_camera_food_reorder(
                product_key, reorder_below=body.reorder_below, reorder_quantity=body.reorder_quantity,
            )
        except KeyError:
            raise HTTPException(404, f"Unknown inventory item '{product_key}'.") from None
        row.reorder_below = body.reorder_below
        row.reorder_quantity = body.reorder_quantity
        row.usual_quantity = body.reorder_quantity
        # A new threshold may change whether the current count is already "low".
        if row.quantity is not None:
            row.level = vision.level_for(float(row.quantity), float(body.reorder_below))
        view = row.model_dump(mode="json")
    state.log(
        None, "shelf",
        f"Reorder settings for {view['name']}: order when ≤ {float(body.reorder_below):g} visible, "
        f"order {float(body.reorder_quantity):g} {view.get('unit') or 'pcs'}.",
    )
    return {"shelf": view}


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC / "index.html")
