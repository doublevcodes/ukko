"""Vercel entrypoint: Ukko + Valio gateway on one deployment.

Locally you still run the two uvicorn processes from the README. On Vercel this
file seeds writable state under /tmp, starts both apps' lifespans, mounts the
gateway at /gateway, and wires Ukko to the gateway in-process (no HTTP loop).
"""

from __future__ import annotations

import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _prepare_runtime() -> None:
    """On Vercel, copy seed data/config into a writable /tmp tree before imports."""
    if not os.getenv("VERCEL"):
        return

    data = Path(os.environ.get("UKKO_DATA_DIR") or "/tmp/ukko-data")
    data.mkdir(parents=True, exist_ok=True)
    bundle = ROOT / "deploy_bundle"
    for name in ("delivery_reliability.csv", "carbon_footprint.csv", "catalogue_index.json"):
        src, dst = bundle / name, data / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)

    config_dir = data / "config"
    if not config_dir.exists():
        shutil.copytree(ROOT / "config", config_dir)

    os.environ["UKKO_DATA_DIR"] = str(data)
    os.environ["UKKO_CONFIG_DIR"] = str(config_dir)

    host = (os.getenv("VERCEL_PROJECT_PRODUCTION_URL") or os.getenv("VERCEL_URL") or "").strip()
    if host and not os.getenv("PUBLIC_BASE_URL"):
        os.environ["PUBLIC_BASE_URL"] = f"https://{host}"
    if not os.getenv("GATEWAY_URL"):
        # Shown in /health and logs; real calls use the in-process client below.
        os.environ["GATEWAY_URL"] = f"https://{host}/gateway" if host else "http://gateway"


_prepare_runtime()

from fastapi import FastAPI
from starlette.testclient import TestClient

from app import gateway_client
from app.main import app as ukko_app
from gateway.main import app as gateway_app

_ukko_lifespan = ukko_app.router.lifespan_context


@asynccontextmanager
async def _combined_lifespan(app: FastAPI):
    async with gateway_app.router.lifespan_context(gateway_app):
        # Sync in-process calls (gateway_client is sync httpx-style). Lifespan already ran above.
        gateway_client.use_client(TestClient(gateway_app))
        async with _ukko_lifespan(app):
            yield


ukko_app.router.lifespan_context = _combined_lifespan
ukko_app.mount("/gateway", gateway_app)

# Vercel loads `app` from this module (see pyproject.toml tool.vercel.entrypoint).
app = ukko_app
