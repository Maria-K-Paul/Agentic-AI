"""Three agents. A supervisor talks to the student and delegates to two specialists.

    student ──▶ supervisor ──ask_programme──▶ programme agent  (search_events, get_event)
                           └─ask_desk───────▶ desk agent       (get_student, check_eligibility,
                                                                register_for_event*, join_waitlist*,
                                                                cancel_registration*, notify_student*)
                                                                * side effects: run once per key

Each specialist is an ordinary agent loop with its own system prompt and its own small tool set. To
the supervisor a specialist is just a tool: "agent as tool", the simplest multi-agent pattern.

Least privilege is structural, not instructed. The programme agent is constructed with
ProgrammeTools, which has no write tools, so no prompt and no jailbreak can make it take a seat.
The desk agent is constructed with a roll number and its tools have no parameter for one, so it
cannot act for a different student.
"""
import time
from collections.abc import Callable

from app.events_db import EventsDb
from app.idempotency import idempotency_key
from app.providers import AgentError
from app.tools.event_tools import DeskTools, ProgrammeTools, Toolset

# A model turn and each of its tool calls count one step. The longest honest path here is the desk
# trying to register, being told the event is full, joining the waitlist and texting: eight.
SPECIALIST_MAX_STEPS = 8

SUPERVISOR_SYSTEM = """You are the Campus Events Assistant, talking to the student with roll number {roll_no}.
You never look at the calendar or change registrations yourself. Delegate:
- ask_programme for finding events and checking how many seats are free;
- ask_desk for anything about this student's record, registrations, waitlist places or messages.
Give each specialist a complete, specific request, including event ids once you know them.
Then answer the student briefly, using only what the specialists reported."""

PROGRAMME_SYSTEM = """You are the programme specialist of a campus events office. Find events and
report their event_id, title, venue, start time and seats_available. You cannot register anyone.
Be brief."""

DESK_SYSTEM = """You are the registration desk specialist, acting for student {roll_no} only.
Always call check_eligibility before register_for_event. If registration comes back with no_seats,
offer join_waitlist for that event. Never decide policy yourself: report the reasons the tools give.
Confirm a successful registration or waitlist place with notify_student. Report what you did, briefly."""


def run_tool(toolset: Toolset, db: EventsDb, key: str, name: str, args: dict) -> tuple[dict, bool]:
    """Run one tool call for any agent. Returns (result, replayed). Never raises, except AgentError.

    Side effects run at most once per key; `replayed` is True when the stored result was returned and
    nothing was done. Delegations hand the key down, so a specialist's side effects get keys derived
    from it, and a replayed delegation replays its side effects safely too.

    A tool that throws is turned into a result the model can read and recover from, because an
    exception that reaches the run loop ends the run, and most tool failures do not deserve that.
    """
    try:
        if name in toolset.DELEGATES:
            return toolset.delegate(name, args, key), False
        if name in toolset.SIDE_EFFECTS:
            result, fresh = db.once(key, name, lambda: toolset.call(name, args))
            return result, not fresh
        return toolset.call(name, args), False
    except AgentError:
        raise
    except NotImplementedError:
        return {"error": "not_implemented", "hint": f"{name} is not available yet."}, False
    except Exception as e:
        return {"error": "tool_failed",
                "hint": f"{name} failed ({type(e).__name__}). Try another way or tell the student."}, False


def run_specialist(agent: str, system: str, toolset: Toolset, *, db: EventsDb, provider, task: str,
                   parent_key: str, on_step: Callable[[dict], None] | None = None) -> dict:
    """A specialist's whole agent loop, run inside one tool call of the supervisor.

    The step limit is what stops a specialist that has decided to call the same tool forever from
    burning the run's budget; the supervisor gets an error result and can tell the student.
    """
    contents = [{"role": "user", "text": task}]
    functions = list(toolset.functions().values())
    used: list[str] = []
    seq = 0
    while seq < SPECIALIST_MAX_STEPS:
        turn = provider.generate(system, contents, functions)
        seq += 1
        if not turn.tool_calls:
            return {"agent": agent, "answer": turn.text or "", "tools_used": used}
        contents.append({"role": "model", "text": turn.text, "raw": turn.raw,
                         "tool_calls": [{"name": c.name, "args": c.args} for c in turn.tool_calls]})
        for call in turn.tool_calls:
            seq += 1
            key = idempotency_key(parent_key, seq, call.name, call.args)
            started = time.perf_counter()
            result, replayed = run_tool(toolset, db, key, call.name, call.args)
            used.append(call.name)
            if on_step:
                on_step({"agent": agent, "kind": "tool", "tool": call.name, "args": call.args,
                         "result": result, "ok": "error" not in result, "replayed": replayed,
                         "ms": round((time.perf_counter() - started) * 1000)})
            contents.append({"role": "tool", "name": call.name, "result": result})
    return {"agent": agent, "error": "specialist_step_limit", "tools_used": used,
            "hint": "The specialist could not finish. Tell the student to try a simpler request."}


class SupervisorTools(Toolset):
    """The supervisor's only tools are the two specialists. It has no database access of its own."""

    TOOL_NAMES = ("ask_programme", "ask_desk")
    DELEGATES = ("ask_programme", "ask_desk")

    def __init__(self, db: EventsDb, providers: dict, roll_no: str, on_step=None):
        self.db, self.providers, self.roll_no, self.on_step = db, providers, roll_no, on_step

    def ask_programme(self, question: str) -> dict:
        """Ask the programme specialist to find events or check how many seats are free.

        Use for "what is on this week", "is the Robotics Bootcamp still open", "any workshops on
        security". It is read-only: it cannot register anyone or hold a seat, so never ask it to.
        It also knows nothing about this student, so do not ask it whether they are eligible.

        Args:
            question: A complete request, e.g. "Is the Robotics Bootcamp still open?"

        Returns:
            {"agent": "programme", "answer": str, "tools_used": [str]}.
        """
        raise RuntimeError("delegations run through delegate()")

    def ask_desk(self, request: str) -> dict:
        """Ask the registration desk specialist to act on this student's record. It CAN CHANGE DATA:
        take and give back seats, join waitlists and send the student messages.

        Use for registering, withdrawing, waitlists, "what am I signed up for", and confirmations.
        Include the event_id from the programme specialist when registering. The desk always acts for
        the current student only, so never name another roll number in the request.

        Args:
            request: A complete instruction, e.g. "Register for event 2 and text the student."

        Returns:
            {"agent": "desk", "answer": str, "tools_used": [str]}.
        """
        raise RuntimeError("delegations run through delegate()")

    def delegate(self, name: str, args: dict, key: str) -> dict:
        bad = self.call_check(name, args)
        if bad:
            return bad
        if self.on_step:
            self.on_step({"agent": "supervisor", "kind": "delegate", "tool": name, "args": args})
        if name == "ask_programme":
            return run_specialist("programme", PROGRAMME_SYSTEM, ProgrammeTools(self.db), db=self.db,
                                  provider=self.providers["programme"], task=args["question"],
                                  parent_key=key, on_step=self.on_step)
        return run_specialist("desk", DESK_SYSTEM.format(roll_no=self.roll_no),
                              DeskTools(self.db, self.roll_no), db=self.db,
                              provider=self.providers["desk"], task=args["request"],
                              parent_key=key, on_step=self.on_step)

    def call_check(self, name: str, args: dict) -> dict | None:
        """Validate a delegation's arguments the same way dispatch validates any other tool call."""
        field = "question" if name == "ask_programme" else "request"
        if set(args) != {field} or not isinstance(args[field], str) or not args[field].strip():
            return {"error": "invalid_arguments", "hint": f"{name} takes one non-empty string: {field}."}
        return None
