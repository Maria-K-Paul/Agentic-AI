"""The Gemini adapter, tested without a key.

`GeminiProvider.__init__` builds a client and therefore needs a key, but `_to_gemini` is pure
translation and is where the format mistakes live. These tests cover it, so the only thing the
`--real` path leaves unverified is the network call itself.
"""
import pytest

from app.providers import GeminiProvider, ModelTurn, RoutedMock, ToolCall

pytest.importorskip("google.genai")


def adapter() -> GeminiProvider:
    """A provider without its client: __new__ skips the __init__ that would need a key."""
    return GeminiProvider.__new__(GeminiProvider)


def test_parallel_tool_results_travel_in_one_turn():
    """Gemini rejects a conversation where two function responses to one model turn are split."""
    out = adapter()._to_gemini([
        {"role": "user", "text": "sign me up"},
        {"role": "model", "text": None, "tool_calls": [
            {"name": "register_for_event", "args": {"event_id": 2}},
            {"name": "notify_student", "args": {"message": "You are in."}}]},
        {"role": "tool", "name": "register_for_event", "result": {"status": "registered"}},
        {"role": "tool", "name": "notify_student", "result": {"status": "queued"}},
    ])
    assert [c.role for c in out] == ["user", "model", "user"]
    assert len(out[1].parts) == 2        # the two calls
    assert len(out[2].parts) == 2        # and their two results, together


def test_a_recorded_model_turn_is_sent_back_unchanged():
    """`raw` carries Gemini's own content, including thought signatures, which must survive a
    round trip through our own representation or the next call is rejected."""
    sentinel = object()
    out = adapter()._to_gemini([
        {"role": "user", "text": "hello"},
        {"role": "model", "text": "ignored", "raw": sentinel, "tool_calls": []},
    ])
    assert out[1] is sentinel


def test_a_rebuilt_model_turn_without_raw_is_reconstructed():
    """After a crash the turn is rebuilt from the database, where `raw` was never stored."""
    out = adapter()._to_gemini([
        {"role": "user", "text": "hello"},
        {"role": "model", "text": "thinking", "tool_calls": [{"name": "get_event", "args": {"event_id": 2}}]},
    ])
    assert out[1].role == "model" and len(out[1].parts) == 2
    assert out[1].parts[1].function_call.name == "get_event"


def test_the_routed_mock_answers_by_position_not_by_call_count():
    """Why the crash drill is honest: a fresh process that resumes a run gets the next turn."""
    mock = RoutedMock({"robotics": [ModelTurn(text=None, tool_calls=[ToolCall("a", {})]),
                                    ModelTurn(text="done")]})
    first = [{"role": "user", "text": "robotics please"}]
    assert mock.generate("s", first, []).tool_calls[0].name == "a"
    resumed = first + [{"role": "model", "text": None, "tool_calls": [{"name": "a", "args": {}}]},
                       {"role": "tool", "name": "a", "result": {}}]
    assert mock.generate("s", resumed, []).text == "done"


def test_an_unscripted_request_is_answered_rather_than_crashing():
    mock = RoutedMock({"robotics": [ModelTurn(text="ok")]})
    assert "no script" in mock.generate("s", [{"role": "user", "text": "something else"}], []).text
