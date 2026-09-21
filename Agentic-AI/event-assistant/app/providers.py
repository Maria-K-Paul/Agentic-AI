"""Model providers. The agent only knows `generate`.

Everything except the final real-model check runs on the scripted providers at the bottom of this
file. They need no key and no network, they answer the same way every time, and they let the tests
put the model in a state a real one would only reach by accident.
"""
from dataclasses import dataclass, field
from typing import Any


class AgentError(Exception):
    """A run could not finish. `retryable` says whether trying again later could work."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class ModelTurn:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    raw: Any = None     # provider-native content, sent back as-is (keeps Gemini's thought signatures)


# `contents` is a plain list the agent builds up:
#   {"role": "user",  "text": str}
#   {"role": "model", "text": str | None, "tool_calls": [{"name", "args"}], "raw": ...}
#   {"role": "tool",  "name": str, "result": dict}


class GeminiProvider:
    def __init__(self, model: str):
        from google import genai

        self.client = genai.Client()        # reads GEMINI_API_KEY
        self.model = model

    def _to_gemini(self, contents: list[dict]):
        from google.genai import types

        out: list = []
        for c in contents:
            if c["role"] == "user":
                out.append(types.Content(role="user", parts=[types.Part.from_text(text=c["text"])]))
            elif c["role"] == "model":
                if c.get("raw") is not None:
                    out.append(c["raw"])
                    continue
                parts = [types.Part.from_text(text=c["text"])] if c.get("text") else []
                parts += [types.Part.from_function_call(name=t["name"], args=t["args"])
                          for t in c.get("tool_calls", [])]
                out.append(types.Content(role="model", parts=parts))
            elif c["role"] == "tool":
                part = types.Part.from_function_response(name=c["name"], response=c["result"])
                # Results of parallel calls travel together in one turn.
                if out and out[-1].role == "user" and all(p.function_response for p in out[-1].parts):
                    out[-1].parts.append(part)
                else:
                    out.append(types.Content(role="user", parts=[part]))
        return out

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=tools,
            temperature=0,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            resp = self.client.models.generate_content(
                model=self.model, contents=self._to_gemini(contents), config=config)
        except errors.APIError as e:
            if e.code == 429:
                raise AgentError("provider_rate_limited", "Model quota exhausted. Wait a minute.", True) from e
            if e.code and e.code >= 500:
                raise AgentError("provider_unavailable", "Model provider failed.", True) from e
            raise AgentError("provider_error", str(e), False) from e

        content = resp.candidates[0].content if resp.candidates else None
        parts = (content.parts or []) if content else []
        text = "".join(p.text for p in parts if p.text and not p.thought) or None
        calls = [ToolCall(fc.name, dict(fc.args or {})) for fc in (resp.function_calls or [])]
        usage = resp.usage_metadata
        return ModelTurn(text=text, tool_calls=calls,
                         tokens_in=(usage.prompt_token_count or 0) if usage else 0,
                         tokens_out=(usage.candidates_token_count or 0) if usage else 0,
                         raw=content)


class ScriptedProvider:
    """Replays a fixed list of turns in call order. No network, no quota. Used by the tests."""

    model = "mock"

    def __init__(self, script: list, loop: bool = False):
        self.original, self.script, self.loop = list(script), list(script), loop
        self.calls: list[list[dict]] = []      # what the agent sent on each call

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        self.calls.append([dict(c) for c in contents])
        if not self.script and self.loop:
            self.script = list(self.original)
        if not self.script:
            return ModelTurn(text="(mock) script exhausted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class PositionalMock:
    """A scripted model that answers by position in the current turn, not by call count.

    This is what makes the crash drill honest: a fresh process that resumes a half-finished run
    rebuilds the conversation from the database and gets the NEXT turn, not the first one, exactly
    as a real model would. `slow` sleeps before each answer, so you have time to kill the worker.
    """

    model = "mock"

    def __init__(self, turns: list[ModelTurn], slow: float = 0.0):
        self.turns, self.slow = turns, slow
        self.calls: list[list[dict]] = []

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        import time

        self.calls.append([dict(c) for c in contents])
        last_user = max(i for i, c in enumerate(contents) if c["role"] == "user")
        position = sum(1 for c in contents[last_user:] if c["role"] == "model")
        if self.slow:
            time.sleep(self.slow)
        if position >= len(self.turns):
            return ModelTurn(text="(mock) nothing more to do.")
        return self.turns[position]


class RoutedMock:
    """Several scripted conversations in one mock: picks a script by a phrase in the current request,
    then answers by position (like PositionalMock). Used by the demo and the tests."""

    model = "mock"

    def __init__(self, routes: dict[str, list[ModelTurn]], slow: float = 0.0):
        self.routes, self.slow = routes, slow
        self.calls: list[list[dict]] = []

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        import time

        self.calls.append([dict(c) for c in contents])
        last_user = max(i for i, c in enumerate(contents) if c["role"] == "user")
        request = contents[last_user]["text"]
        position = sum(1 for c in contents[last_user:] if c["role"] == "model")
        if self.slow:
            time.sleep(self.slow)
        for phrase, turns in self.routes.items():
            if phrase.lower() in request.lower():
                return turns[position] if position < len(turns) else ModelTurn(text="(mock) done.")
        return ModelTurn(text="(mock) I have no script for that request.")


def _call(name, **args):
    return ModelTurn(text=None, tool_calls=[ToolCall(name, args)], tokens_in=100, tokens_out=10)


CONFIRM_BOOTCAMP = "You are registered for the Robotics Bootcamp on 11 Oct at the Robotics Lab."
CONFIRM_WAITLIST = "You are number 1 on the waitlist for the Cloud Security Workshop."


def demo_providers(slow: float = 0.0) -> dict:
    """Scripted models for the three agents, covering the demo's three questions.

    The supervisor routes on the student's own words, each specialist on the request it was handed.
    """
    return {
        "supervisor": RoutedMock({
            "Robotics Bootcamp": [
                _call("ask_programme", question="Is the Robotics Bootcamp still open?"),
                _call("ask_desk", request="Register for event 2 (Robotics Bootcamp) and text the student to confirm."),
                ModelTurn(text="(mock) Good news: one seat was left, it is yours, and a confirmation text is on its way."),
            ],
            "Intro to Generative AI": [
                _call("ask_programme", question="Find Intro to Generative AI"),
                _call("ask_desk", request="Register for event 1 (Intro to Generative AI)."),
                ModelTurn(text="(mock) Intro to Generative AI has seats, but the desk cannot sign you up: your attendance is 62% and the office requires 75%."),
            ],
            "Cloud Security": [
                _call("ask_programme", question="Is the Cloud Security Workshop open?"),
                _call("ask_desk", request="Register for event 4 (Cloud Security Workshop); if it is full, put the student on the waitlist and text them."),
                ModelTurn(text="(mock) The Cloud Security Workshop is full, so I put you on the waitlist at number 1 and texted you."),
            ],
        }, slow),
        "programme": RoutedMock({
            "Robotics Bootcamp": [_call("search_events", text="Robotics Bootcamp"),
                                  ModelTurn(text="(mock) Event 2, Robotics Bootcamp, Robotics Lab, 11 Oct: 1 seat left.")],
            "Intro to Generative AI": [_call("search_events", text="Generative AI"),
                                       ModelTurn(text="(mock) Event 1, Intro to Generative AI, Auditorium A, 10 Oct: 40 seats left.")],
            "Cloud Security": [_call("search_events", text="Cloud Security"),
                               ModelTurn(text="(mock) Event 4, Cloud Security Workshop, Lab C, 17 Oct: 0 seats left, it is full.")],
        }, slow),
        "desk": RoutedMock({
            "event 2": [
                _call("check_eligibility"),
                ModelTurn(text=None, tool_calls=[
                    ToolCall("register_for_event", {"event_id": 2}),
                    ToolCall("notify_student", {"message": CONFIRM_BOOTCAMP})],
                    tokens_in=150, tokens_out=25),
                ModelTurn(text="(mock) Registered for event 2 and sent the confirmation."),
            ],
            "event 1": [
                _call("check_eligibility"),
                ModelTurn(text="(mock) Not registered: the student's attendance of 62% is below the 75% the office requires."),
            ],
            "event 4": [
                _call("check_eligibility"),
                _call("register_for_event", event_id=4),
                ModelTurn(text=None, tool_calls=[
                    ToolCall("join_waitlist", {"event_id": 4}),
                    ToolCall("notify_student", {"message": CONFIRM_WAITLIST})],
                    tokens_in=170, tokens_out=30),
                ModelTurn(text="(mock) Event 4 was full, so I added the student to the waitlist at position 1 and texted them."),
            ],
        }, slow),
    }
