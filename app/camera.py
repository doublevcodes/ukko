"""The shelf camera's video, read once and shared.

One background thread holds a single connection to the camera's MJPEG stream
(CAMERA_FEED_URL) and remembers the newest picture. The dashboard's feed
(GET /camera/feed) and the vision loop both use that picture, so the camera
serves one client however many dashboards are open.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
import time
from collections.abc import AsyncIterator
from types import FrameType
from typing import Any

import httpx

from app import settings

SOI, EOI = b"\xff\xd8", b"\xff\xd9"  # the bytes that open and close every JPEG picture
BOUNDARY = "frame"
FRESH_SECONDS = 5.0     # no new picture for this long means the camera has stopped sending
RETRY_SECONDS = 3.0
MAX_BUFFER = 4_000_000  # a "picture" longer than this is not one; start again
log = logging.getLogger("stockpot.camera")

_lock = threading.Lock()
_frame: bytes | None = None
_frame_at = 0.0  # time.monotonic() when the newest picture arrived
_count = 0       # pictures received so far, so a reader can tell a new one from the one it has
_started = False
_stopping = False  # True once Stockpot was told to stop: the open video streams end


def split_frames(buffer: bytearray) -> list[bytes]:
    """Take every complete JPEG out of `buffer` and leave the unfinished rest in it.

    The pictures are found by their own start and end bytes, not by the stream's
    part headers, so it does not matter how the camera words those headers.
    """
    frames = []
    while True:
        start = buffer.find(SOI)
        if start < 0:
            del buffer[:-1]  # keep the last byte: it may be the first half of a start marker
            return frames
        end = buffer.find(EOI, start + 2)
        if end < 0:
            del buffer[:start]
            if len(buffer) > MAX_BUFFER:
                buffer.clear()
            return frames
        frames.append(bytes(buffer[start:end + 2]))
        del buffer[:end + 2]


def _remember(frame: bytes) -> None:
    global _frame, _frame_at, _count
    with _lock:
        _frame, _frame_at, _count = frame, time.monotonic(), _count + 1


def newest() -> tuple[bytes | None, int]:
    """The newest picture and its number, or (None, number) when the camera has stopped sending."""
    with _lock:
        fresh = _frame is not None and time.monotonic() - _frame_at <= FRESH_SECONDS
        return (_frame if fresh else None), _count


def latest_frame() -> bytes | None:
    return newest()[0]


def connected() -> bool:
    return latest_frame() is not None


def _read_stream(url: str) -> None:
    """Read pictures from the camera until the stream ends or breaks."""
    buffer = bytearray()
    with httpx.stream("GET", url, timeout=httpx.Timeout(FRESH_SECONDS)) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            buffer.extend(chunk)
            for frame in split_frames(buffer):
                _remember(frame)


def _read_forever(url: str) -> None:
    """Stay connected for as long as Stockpot runs. A camera that is off is reported once, not every retry."""
    reported = False
    while True:
        before = _count
        try:
            _read_stream(url)
            problem = "the camera closed the stream"
        except Exception as error:  # noqa: BLE001  whatever went wrong, the answer is the same: try again
            problem = f"{type(error).__name__}: {error}"
        if _count != before:
            reported = False  # pictures were arriving, so this is a new interruption
        if not reported:
            log.warning("No video from the camera at %s (%s). Trying again every %.0f seconds.", url, problem, RETRY_SECONDS)
            reported = True
        time.sleep(RETRY_SECONDS)


def start() -> None:
    """Begin reading the camera in the background. Does nothing without CAMERA_FEED_URL."""
    global _started
    if _started or not settings.CAMERA_FEED_URL:
        return
    _started = True
    end_relays_on_exit()
    threading.Thread(target=_read_forever, args=(settings.CAMERA_FEED_URL,), name="camera", daemon=True).start()


def end_relays_on_exit() -> None:
    """Make Ctrl+C end the open video streams, so one Ctrl+C still stops Stockpot.

    uvicorn waits for open connections before it exits, and a video stream never
    ends by itself: without this the server hangs until every dashboard is closed.
    The server's own handler still runs afterwards. Signal handlers can only be set
    from the main thread, which is where uvicorn starts the app.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    for number in (signal.SIGINT, signal.SIGTERM):
        servers_handler = signal.getsignal(number)
        if not callable(servers_handler):
            continue  # nothing to pass the signal on to, so leave it alone

        def on_exit(signum: int, frame: FrameType | None, then: Any = servers_handler) -> None:
            global _stopping
            _stopping = True
            then(signum, frame)

        signal.signal(number, on_exit)


async def relay() -> AsyncIterator[bytes]:
    """The video as an MJPEG stream of our own: one part per new picture. Ends when the camera goes quiet."""
    sent = 0
    while not _stopping:
        frame, count = newest()
        if frame is None:
            return
        if count == sent:
            await asyncio.sleep(0.03)
            continue
        sent = count
        yield b"--%b\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%b\r\n" % (BOUNDARY.encode(), len(frame), frame)
