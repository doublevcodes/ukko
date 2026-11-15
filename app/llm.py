"""Every language-model call goes through this file (Google Gemini).

To switch provider, change only this file. The rest of the code calls
`complete_json`, `complete_json_with_image` and `complete_text` and handles `LLMError`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from google import genai
from google.genai import types

from app import settings

# The google-genai SDK takes its timeout in milliseconds. With the low thinking
# level a call takes 2 to 3 seconds, so 10 is generous and keeps the demo moving.
TIMEOUT_MS = 10_000
ATTEMPTS = 2  # one retry, and only when the service was slow or had a problem of its own
_client: genai.Client | None = None
log = logging.getLogger("ukko.llm")


class LLMError(Exception):
    """The language model could not be used. The message is plain English and safe to show."""


def _gemini() -> genai.Client:
    global _client
    if not settings.GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY is not set in .env, so the language model cannot be used.")
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def _thinking() -> dict[str, Any]:
    """How long the model may think before answering. Blank means the model's own default."""
    level = settings.GEMINI_THINKING_LEVEL.strip().upper()
    if not level:
        return {}
    # The SDK only warns about a name it does not know, so the check is made here.
    if level not in types.ThinkingLevel.__members__:
        raise LLMError("GEMINI_THINKING_LEVEL in .env is not a level Gemini knows (try: low).")
    return {"thinking_config": types.ThinkingConfig(thinking_level=types.ThinkingLevel[level])}


def _in_plain_words(error: Exception) -> tuple[str, bool]:
    """(what to tell a person, whether trying again could help). The raw error goes to the server log."""
    code = getattr(error, "code", None)  # the HTTP status, on the SDK's own errors
    if isinstance(error, httpx.TimeoutException) or code == 504:
        return "The language model took too long to answer.", True
    if isinstance(error, httpx.TransportError):
        return "The language model could not be reached (network problem).", True
    if code == 429:
        return "The language model's usage limit was reached.", False
    if code in (401, 403):
        return "The language model refused the key in GEMINI_API_KEY.", False
    if isinstance(code, int) and code >= 500:
        return "The language model service had a problem.", True
    return "The language model rejected the request. Check GEMINI_API_KEY and GEMINI_MODEL in .env.", False


def _generate(system: str, user: Any, **config: Any) -> str:
    """One Gemini call with a low temperature, so answers are steady. `user` is text, or a list of pictures and text."""
    settings_for_call = types.GenerateContentConfig(
        system_instruction=system, temperature=0.1,
        http_options=types.HttpOptions(timeout=TIMEOUT_MS), **_thinking(), **config,
    )
    client = _gemini()
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = client.models.generate_content(model=settings.GEMINI_MODEL, contents=user, config=settings_for_call)
        except Exception as error:  # noqa: BLE001  network, quota, bad key, bad model name: all mean "no answer"
            message, worth_retrying = _in_plain_words(error)
            log.warning("Gemini call failed (attempt %s of %s): %s: %s", attempt, ATTEMPTS, type(error).__name__, str(error)[:300])
            if worth_retrying and attempt < ATTEMPTS:
                continue
            raise LLMError(message) from error
        if not response.text:
            raise LLMError("The language model returned an empty answer.")
        return response.text
    raise LLMError("The language model gave no answer.")  # not reachable; keeps the return type honest


def _json_answer(system: str, user: Any, schema: dict[str, Any]) -> dict[str, Any]:
    text = _generate(system, user, response_mime_type="application/json", response_json_schema=schema)
    try:
        answer = json.loads(text)
    except ValueError as error:
        raise LLMError("The language model did not return valid JSON.") from error
    if not isinstance(answer, dict):
        raise LLMError("The language model returned JSON in an unexpected shape.")
    return answer


def complete_json(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Ask for an answer as JSON that follows `schema` (a JSON Schema object)."""
    return _json_answer(system, user, schema)


def complete_json_with_image(system: str, user: str, schema: dict[str, Any], image: bytes,
                             mime_type: str = "image/jpeg") -> dict[str, Any]:
    """Like `complete_json`, with one picture for the model to look at."""
    return _json_answer(system, [types.Part.from_bytes(data=image, mime_type=mime_type), user], schema)


def complete_text(system: str, user: str) -> str:
    """Ask for a short plain-text answer."""
    return _generate(system, user).strip()
