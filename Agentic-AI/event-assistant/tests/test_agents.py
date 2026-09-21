"""A supervisor that delegates to two specialists, and the keys it hands down."""
from app.agents import SupervisorTools, run_specialist, run_tool
from app.providers import ModelTurn, ScriptedProvider, ToolCall, demo_providers
from app.tools.event_tools import DeskTools, ProgrammeTools

REGISTER = {"request": "Register for event 2 (Robotics Bootcamp) and text the student to confirm."}


def test_the_supervisor_only_has_delegation_tools(db):
    """It cannot touch the database itself; everything goes through a specialist."""
    tools = SupervisorTools(db, demo_providers(), "23CS101")
    assert set(tools.functions()) == {"ask_programme", "ask_desk"}
    assert set(tools.DELEGATES) == set(tools.TOOL_NAMES)


def test_the_programme_specialist_answers_a_question(db):
    result, replayed = run_tool(SupervisorTools(db, demo_providers(), "23CS101"), db, "k1",
                                "ask_programme", {"question": "Is the Robotics Bootcamp still open?"})
    assert result["agent"] == "programme" and result["tools_used"] == ["search_events"]
    assert replayed is False and "1 seat left" in result["answer"]


def test_the_desk_specialist_registers_and_notifies_for_the_bound_student(db):
    result, _ = run_tool(SupervisorTools(db, demo_providers(), "23CS101"), db, "k1", "ask_desk", REGISTER)
    assert result["tools_used"] == ["check_eligibility", "register_for_event", "notify_student"]
    assert [r["event_id"] for r in db.active_registrations(1)] == [2]
    assert db.count("notification") == 1


def test_a_repeated_delegation_with_the_same_key_does_nothing_twice(db):
    """The delegation key is the parent of every key inside it, so replaying the delegation replays
    its side effects instead of redoing them."""
    tools = SupervisorTools(db, demo_providers(), "23CS101")
    run_tool(tools, db, "same-key", "ask_desk", REGISTER)
    run_tool(tools, db, "same-key", "ask_desk", REGISTER)
    assert db.count("registration") == 2          # the seed's one, plus exactly one new
    assert db.count("notification") == 1
    assert db.count("idempotency") == 2           # one per side effect, not per attempt
    assert db.get_event(2)["seats_available"] == 0


def test_a_different_key_still_cannot_take_a_second_seat(db):
    """Keys make a repeat free; the unique constraint makes it safe even when the key differs."""
    tools = SupervisorTools(db, demo_providers(), "23CS101")
    run_tool(tools, db, "key-a", "ask_desk", REGISTER)
    run_tool(tools, db, "key-b", "ask_desk", REGISTER)
    assert db.count("registration") == 2 and db.get_event(2)["seats_available"] == 0


def test_bad_delegation_arguments_are_fed_back_not_raised(db):
    result, _ = run_tool(SupervisorTools(db, demo_providers(), "23CS101"), db, "k", "ask_desk",
                         {"request": ""})
    assert result["error"] == "invalid_arguments"


def test_a_tool_that_throws_becomes_a_result_the_model_can_read(db):
    """An exception that reached the run loop would end the run; most tool failures do not deserve that."""
    result, _ = run_tool(DeskTools(db, "not-a-student"), db, "k", "get_student", {})
    assert result["error"] == "tool_failed" and "get_student" in result["hint"]


def test_an_unknown_tool_name_is_answered_not_raised(db):
    result, _ = run_tool(ProgrammeTools(db), db, "k", "search_eventz", {"text": "robotics"})
    assert result["error"] == "unknown_tool"


def test_a_looping_specialist_stops(db):
    looping = ScriptedProvider(
        [ModelTurn(text=None, tool_calls=[ToolCall("search_events", {"text": "robotics"})])], loop=True)
    result = run_specialist("programme", "sys", ProgrammeTools(db), db=db, provider=looping,
                            task="x", parent_key="k")
    assert result["error"] == "specialist_step_limit"


def test_specialists_see_only_their_own_task(db):
    """A specialist gets the request, not the conversation. It cannot leak what it never saw."""
    providers = demo_providers()
    run_tool(SupervisorTools(db, providers, "23CS101"), db, "k", "ask_programme",
             {"question": "Is the Robotics Bootcamp still open?"})
    assert providers["programme"].calls[0] == [
        {"role": "user", "text": "Is the Robotics Bootcamp still open?"}]
    assert providers["desk"].calls == []


def test_the_desk_is_bound_to_the_supervisors_student(db):
    """The roll number comes from the thread, never from anything the model wrote."""
    run_tool(SupervisorTools(db, demo_providers(), "23ME018"), db, "k", "ask_desk", REGISTER)
    assert [r["event_id"] for r in db.active_registrations(1)] == []       # not Meera's
    assert db.count("registration") == 1      # Sana holds 1 of 1 already, so she is refused
