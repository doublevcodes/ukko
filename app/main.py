"""Stockpot buying agent: HTTP routes.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
Dashboard: http://localhost:8000   API description: http://localhost:8000/docs
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import segno
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import authority, camera, categories, config, intake, settings, state, vision
from app.gateway_client import GatewayError, GatewayRefused
from app.models import PolicyConfig, SensorEvent

STATIC = Path(__file__).parent / "static"


class TypedRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    reply_to: str | None = Field(default=None, description="Id of a request whose question this text answers.")


class ApprovalAction(BaseModel):
    action: Literal["approve", "reject"]
    note: str = Field(default="", max_length=300)


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
    _sign_standing_limits("Stockpot started.")
    intake.wire_vision_reorders()
    camera.start()
    vision.start()
    yield


class FreshStaticFiles(StaticFiles):
    """Static files the browser must re-check before reusing its copy.

    Without this a reload can pair a new index.html with an older cached script,
    and the dashboard breaks until someone forces a full refresh.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="Stockpot buying agent", version="0.1.0", lifespan=lifespan)
# The sensor device and a phone on the local network both call this API.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", FreshStaticFiles(directory=STATIC), name="static")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "stockpot", "gateway_url": settings.GATEWAY_URL,
            "gemini_key_set": bool(settings.GEMINI_API_KEY), "exa_key_set": bool(settings.EXA_API_KEY),
            "camera_connected": camera.connected()}


@app.get("/camera/feed", include_in_schema=False)
def camera_feed() -> StreamingResponse:
    """The shelf camera's video for the dashboard, relayed from Stockpot's one connection to the camera."""
    if not camera.connected():
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


@lru_cache(maxsize=64)
def _qr(url: str) -> str:
    return segno.make(url, error="m").svg_inline(scale=4, border=2)


@app.get("/state")
def get_state() -> dict[str, Any]:
    """Everything the dashboard shows, in one call."""
    vision.note_viewer()
    snapshot = state.snapshot()
    for view in snapshot["requests"]:
        token = state.requests[view["id"]].approval_token if view["id"] in state.requests else None
        if token:
            url = f"{settings.public_base_url()}/approve/{token}"
            view["approval"] = {"url": url, "qr_svg": _qr(url)}
    snapshot["standing_limits"] = authority.summary()
    snapshot["category_names"] = categories.all_names()
    snapshot["health"] = health()
    return snapshot


def _decide(record: state.RequestRecord | None, body: ApprovalAction, tasks: BackgroundTasks, where: str) -> dict[str, Any]:
    if record is None:
        raise HTTPException(404, "No such request, or this approval link has already been used.")
    if not intake.claim_approval(record):
        raise HTTPException(409, "This request is not waiting for approval.")
    if body.action == "approve":
        tasks.add_task(intake.run_approved, record, body.note, where)
    else:
        intake.reject(record, body.note, where)
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


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC / "index.html")
