"""How app/llm.py behaves when Gemini is slow or refuses: plain words, and one retry.

Gemini itself is never called: a stand-in client raises or answers on cue.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors

from app import llm
from app.web import exa_checks


class ScriptedGemini:
    """Stands in for the Gemini client: each call takes the next outcome from the script."""

    def __init__(self, *script: Exception | str) -> None:
        self.script = list(script)
        self.calls = 0
        self.models = SimpleNamespace(generate_content=self._generate_content)

    def _generate_content(self, **_: object) -> SimpleNamespace:
        self.calls += 1
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)


def deadline_exceeded() -> errors.ServerError:
    """The error Gemini really returned in a live run when it ran out of time."""
    return errors.ServerError(504, {"error": {"code": 504, "message": "Deadline expired before operation could complete.",
                                              "status": "DEADLINE_EXCEEDED"}})


def use(monkeypatch: pytest.MonkeyPatch, gemini: ScriptedGemini) -> ScriptedGemini:
    monkeypatch.setattr(llm, "_gemini", lambda: gemini)
    return gemini


def test_a_timeout_is_reported_in_plain_words_after_one_retry(monkeypatch):
    gemini = use(monkeypatch, ScriptedGemini(deadline_exceeded(), httpx.ReadTimeout("timed out")))
    with pytest.raises(llm.LLMError) as failure:
        llm._generate("system", "user")
    assert str(failure.value) == "The language model took too long to answer."
    assert gemini.calls == 2


def test_a_slow_first_try_is_retried_and_the_answer_is_used(monkeypatch):
    gemini = use(monkeypatch, ScriptedGemini(deadline_exceeded(), "An answer."))
    assert llm._generate("system", "user") == "An answer."
    assert gemini.calls == 2


def test_a_usage_limit_is_not_retried(monkeypatch):
    gemini = use(monkeypatch, ScriptedGemini(errors.ClientError(429, {"error": {"code": 429, "message": "Quota exceeded."}})))
    with pytest.raises(llm.LLMError, match="usage limit"):
        llm._generate("system", "user")
    assert gemini.calls == 1


def test_an_unknown_thinking_level_is_explained(monkeypatch):
    use(monkeypatch, ScriptedGemini("unused"))
    monkeypatch.setattr(llm.settings, "GEMINI_THINKING_LEVEL", "enormous")
    with pytest.raises(llm.LLMError, match="GEMINI_THINKING_LEVEL"):
        llm._generate("system", "user")


def test_a_failed_search_is_reported_without_raw_error_text(world):
    world.exa.down = True
    finding = exa_checks.recall_check("Test Milk 1 L", "Test")
    assert finding.severity == "check_failed"
    assert "ConnectionError" not in finding.summary and "switched off" not in finding.summary


def test_the_recall_question_spells_out_the_time_window(world, monkeypatch):
    asked = []
    world.exa.results = [{"title": "Old notice", "url": "https://example.test/a", "published_date": None, "text": "A notice."}]
    monkeypatch.setattr(llm, "complete_json", lambda system, user, schema: asked.append(user) or world.llm.verdict)
    exa_checks.recall_check("Test Milk 1 L", "Test")
    assert "Today is" in asked[0] and "issued on or after" in asked[0]
