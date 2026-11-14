"""Stockpot configuration, read once from the environment (.env)."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

HELSINKI = ZoneInfo("Europe/Helsinki")
# Must match the --port Stockpot is started with; it is used in the phone approval link.
PORT = int(os.getenv("STOCKPOT_PORT") or 8000)

DATA_DIR = Path(os.getenv("STOCKPOT_DATA_DIR") or ROOT / "data")
AUDIT_LOG = DATA_DIR / "audit_log.jsonl"
KEYS_DIR = DATA_DIR / "keys"

CONFIG_DIR = Path(os.getenv("STOCKPOT_CONFIG_DIR") or ROOT / "config")
POLICY_FILE = CONFIG_DIR / "policy.yaml"
SHELF_FILE = CONFIG_DIR / "shelf.yaml"
CAMERA_FOODS_FILE = CONFIG_DIR / "camera_foods.yaml"
CATEGORIES_FILE = CONFIG_DIR / "categories.yaml"

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8001").rstrip("/")
# Who we are to the supplier. BUYER_ID is the customer account; AGENT_ID names this software.
BUYER_ID = os.getenv("BUYER_ID", "stockpot-demo-kitchen")
AGENT_ID = os.getenv("AGENT_ID", "stockpot-agent")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-4-hydrogen"
# "low" answers in about 2 seconds instead of 9, with the same verdicts on our prompts.
# Set it to blank in .env to use the model's own default.
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "low")
EXA_API_KEY = os.getenv("EXA_API_KEY", "")

# The shelf camera's MJPEG stream. Stockpot relays it to the dashboard (GET /camera/feed) and
# looks at its frames to find the products in the picture. Blank switches both off.
CAMERA_FEED_URL = os.getenv("CAMERA_FEED_URL", "http://10.0.41.90:5000/").strip()
# Seconds between two looks at the picture. Each look is one Gemini call, made only while a dashboard is open.
VISION_INTERVAL_SECONDS = float(os.getenv("VISION_INTERVAL_SECONDS") or 3)

# Minutes between two forecast checks that order ahead before an item runs out (app/intake.py).
FORECAST_CHECK_MINUTES = float(os.getenv("FORECAST_CHECK_MINUTES") or 15)


def public_base_url() -> str:
    """The address a phone on the same network can use to reach Stockpot."""
    configured = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if configured:
        return configured
    try:
        # Connecting a UDP socket sends nothing; it only reveals which local
        # address the laptop would use to reach the outside world.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))
            address = probe.getsockname()[0]
    except OSError:
        address = "127.0.0.1"
    return f"http://{address}:{PORT}"
