"""Durable copies of demo state for Vercel.

Locally everything stays on disk under data/. On Vercel that directory is
/tmp and vanishes on every cold start, which wiped the kitchen card, keys and
order history whenever the camera (or anything else) recycled the function.

When BLOB_STORE_ID (or a Blob read-write token) is set, this module mirrors a
small set of files to private Vercel Blob and restores them before the apps
boot.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("ukko.durable")

# Relative to UKKO_DATA_DIR. Keep this list small: every path is pulled on boot.
TRACKED = (
    "stripe_customer.json",
    "audit_log.jsonl",
    "live_state.json",
    "usage_cycles.json",
    "keys/manager.json",
    "keys/agent.json",
    "gateway/buyers.json",
    "gateway/orders.jsonl",
    "gateway/keys/supplier.json",
    "config/policy.yaml",
)

_PREFIX = "ukko-runtime/"
_lock = threading.Lock()
_enabled: bool | None = None
_pending: set[str] = set()
_flush_timer: threading.Timer | None = None
# Card and keys upload immediately; history can wait a moment and coalesce.
_IMMEDIATE = frozenset({
    "stripe_customer.json",
    "keys/manager.json",
    "keys/agent.json",
    "gateway/keys/supplier.json",
    "gateway/buyers.json",
})


def enabled() -> bool:
    """True when Blob credentials are available (Vercel OIDC or a RW token)."""
    global _enabled
    if _enabled is None:
        _enabled = bool(
            os.getenv("BLOB_STORE_ID")
            or os.getenv("BLOB_READ_WRITE_TOKEN")
            or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN")
        )
    return _enabled


def _blob():
    from vercel.blob import sync as blob

    return blob


def _pathname(relative: str) -> str:
    return f"{_PREFIX}{relative.lstrip('/')}"


def _data_dir(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit
    try:
        from app import settings

        return settings.DATA_DIR
    except Exception:  # noqa: BLE001
        return None


def hydrate(data_dir: Path) -> int:
    """Pull tracked files from Blob into data_dir. Returns how many were restored."""
    if not enabled():
        return 0
    blob = _blob()
    restored = 0
    for relative in TRACKED:
        dest = data_dir / relative
        if dest.exists() and dest.stat().st_size > 0:
            continue
        try:
            result = blob.get_blob(_pathname(relative), access="private")
        except Exception as error:  # noqa: BLE001  missing file or cold store is fine
            log.debug("No blob for %s (%s)", relative, error)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(result.body)
        if "keys/" in relative:
            try:
                dest.chmod(0o600)
            except OSError:
                pass
        restored += 1
        log.info("Restored %s from Blob (%d bytes).", relative, len(result.body))
    return restored


def _upload(relative: str, data_dir: Path) -> None:
    path = data_dir / relative
    if not path.exists():
        return
    body = path.read_bytes()
    try:
        _blob().put_blob(
            _pathname(relative),
            body,
            access="private",
            allow_overwrite=True,
            add_random_suffix=False,
            content_type="application/octet-stream",
        )
    except Exception as error:  # noqa: BLE001  never break the demo for a sync failure
        log.warning("Could not mirror %s to Blob: %s", relative, error)


def _flush_pending() -> None:
    global _flush_timer
    with _lock:
        pending = list(_pending)
        _pending.clear()
        _flush_timer = None
    root = _data_dir()
    if root is None:
        return
    for relative in pending:
        _upload(relative, root)


def mirror(path: Path, data_dir: Path | None = None) -> None:
    """Upload one local file to Blob if it is under the tracked data dir."""
    if not enabled():
        return
    root = _data_dir(data_dir)
    if root is None:
        return
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return
    if relative not in TRACKED or not path.exists():
        return
    if relative in _IMMEDIATE:
        with _lock:
            _upload(relative, root)
        return
    global _flush_timer
    with _lock:
        _pending.add(relative)
        if _flush_timer is not None:
            _flush_timer.cancel()
        _flush_timer = threading.Timer(1.5, _flush_pending)
        _flush_timer.daemon = True
        _flush_timer.start()


def mirror_rel(relative: str, data_dir: Path | None = None) -> None:
    """Upload a tracked relative path if the local file exists."""
    root = _data_dir(data_dir)
    if root is None:
        return
    mirror(root / relative, root)
