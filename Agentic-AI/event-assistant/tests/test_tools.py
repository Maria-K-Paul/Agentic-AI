"""The tools, the rules that live in data, and writes that are safe to repeat."""
import inspect

import pytest

from app.tools.event_tools import DeskTools, ProgrammeTools


@pytest.mark.parametrize("cls", [ProgrammeTools, DeskTools])
def test_every_tool_is_described(cls):
    """A description is the only thing the model has when it picks a tool, so it has to say when to
    use it, when not to, and what it changes."""
    for name in cls.TOOL_NAMES:
        doc = inspect.getdoc(getattr(cls, name)) or ""
        assert len(doc) >= 200, f"{cls.__name__}.{name} is under-described"
        assert "Returns:" in doc, f"{cls.__name__}.{name} does not say what it returns"


def test_the_programme_agent_has_no_write_tools():
    assert ProgrammeTools.SIDE_EFFECTS == ()
    assert not set(ProgrammeTools.TOOL_NAMES) & set(DeskTools.SIDE_EFFECTS)


def test_search_finds_events_and_reports_free_seats(db):
    events = ProgrammeTools(db).search_events("robotics")["events"]
    assert [e["event_id"] for e in events] == [2]
    assert events[0]["seats_available"] == 1


def test_search_rejects_an_empty_query(db):
    assert ProgrammeTools(db).search_events("   ")["error"] == "empty_query"


def test_get_event_says_so_when_the_id_is_wrong(db):
    assert ProgrammeTools(db).get_event(99)["error"] == "unknown_event"


def test_policy_comes_from_the_database_not_a_prompt(db):
    """Change the row, and the answer changes. No redeploy, no prompt edit."""
    rahul = DeskTools(db, "23EE042")
    assert rahul.check_eligibility() == {
        "eligible": False, "reasons": ["attendance 62% is below the 75% required"]}
    db.conn.execute("UPDATE policy SET value = 60 WHERE name = 'min_attendance_to_register'")
    assert rahul.check_eligibility()["eligible"] is True


def test_the_registration_limit_is_the_students_own(db):
    assert DeskTools(db, "23ME018").check_eligibility()["reasons"] == [
        "already holds 1 of 1 allowed registrations"]


def test_register_refuses_when_policy_says_no_even_if_the_model_skips_the_check(db):
    """The rule is enforced in the tool, so a model that never calls check_eligibility gains nothing."""
    assert DeskTools(db, "23EE042").register_for_event(1)["error"] == "not_allowed"
    assert db.count("registration") == 1          # only the seed's


def test_registering_twice_is_not_an_error_and_takes_one_seat(db):
    desk = DeskTools(db, "23CS101")
    assert desk.register_for_event(2)["status"] == "registered"
    assert desk.register_for_event(2)["status"] == "already_registered"
    assert db.get_event(2)["seats_available"] == 0


def test_the_last_seat_goes_to_one_student(db):
    assert DeskTools(db, "23CS101").register_for_event(2)["status"] == "registered"
    db.conn.execute("UPDATE student SET max_active_registrations = 3 WHERE roll_no = '23ME018'")
    assert DeskTools(db, "23ME018").register_for_event(2)["error"] == "no_seats"


def test_registering_for_an_unknown_event(db):
    assert DeskTools(db, "23CS101").register_for_event(404)["error"] == "unknown_event"


def test_the_waitlist_is_only_for_full_events(db):
    assert DeskTools(db, "23CS101").join_waitlist(1)["error"] == "has_seats"


def test_joining_the_waitlist_twice_keeps_the_same_place(db):
    desk = DeskTools(db, "23CS101")
    first, second = desk.join_waitlist(4), desk.join_waitlist(4)
    assert first["status"] == "waitlisted" and first["position"] == 1
    assert second["status"] == "already_waitlisted" and second["position"] == 1
    assert db.count("waitlist") == 1


def test_the_waitlist_cap_also_comes_from_the_database(db):
    db.conn.execute("UPDATE event SET seats_available = 0 WHERE id IN (1, 5)")
    desk = DeskTools(db, "23CS101")
    assert desk.join_waitlist(4)["status"] == "waitlisted"
    assert desk.join_waitlist(1)["status"] == "waitlisted"
    assert desk.join_waitlist(5)["error"] == "waitlist_limit"
    db.conn.execute("UPDATE policy SET value = 3 WHERE name = 'max_waitlist_per_student'")
    assert desk.join_waitlist(5)["status"] == "waitlisted"


def test_cancelling_gives_the_seat_back_and_repeats_harmlessly(db):
    desk = DeskTools(db, "23CS101")
    desk.register_for_event(2)
    assert db.get_event(2)["seats_available"] == 0
    assert desk.cancel_registration(2)["status"] == "cancelled"
    assert db.get_event(2)["seats_available"] == 1
    assert desk.cancel_registration(2)["status"] == "not_registered"
    assert db.get_event(2)["seats_available"] == 1        # not 2: the second call gave nothing back


def test_the_same_text_on_the_same_day_is_sent_once(db):
    desk = DeskTools(db, "23CS101")
    first, second = desk.notify_student("You are in."), desk.notify_student("You  are\nin.")
    assert first["notification_id"] == second["notification_id"]
    assert second["duplicate"] is True and db.count("notification") == 1


def test_an_unusable_message_is_refused_before_anything_is_written(db):
    desk = DeskTools(db, "23CS101")
    assert desk.notify_student("   ")["error"] == "invalid_message"
    assert desk.notify_student("x" * 161)["error"] == "invalid_message"
    assert db.count("notification") == 0


def test_the_desk_cannot_act_for_another_student(db):
    """Least privilege by construction: no tool here takes a roll number, so the model cannot pass one."""
    for name in DeskTools.TOOL_NAMES:
        params = list(inspect.signature(getattr(DeskTools, name)).parameters)
        assert "roll_no" not in params and "student_id" not in params, name
