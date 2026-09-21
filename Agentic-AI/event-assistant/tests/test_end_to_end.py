"""The whole thing: queue, worker, supervisor, specialists, crash, replay."""
import pytest

from app.providers import demo_providers
from app.worker import Worker
from tests.conftest import SimulatedCrash

MEERA = "Is the Robotics Bootcamp still open? If it is, sign me up and text me."
RAHUL = "Can I register for Intro to Generative AI?"
FULL = "I also want the Cloud Security Workshop."


def ask(store, roll_no, text):
    thread = store.create_thread(roll_no)
    return thread, store.enqueue(thread, text, "mock")


def test_a_question_goes_all_the_way_through(store, db):
    thread, run_id = ask(store, "23CS101", MEERA)
    assert Worker(store, db, demo_providers(), worker_id="w").run_until_idle() == [(run_id, "succeeded")]
    run = store.get_run(run_id)
    assert [s["tool_name"] for s in run["steps"] if s["kind"] == "tool"] == ["ask_programme", "ask_desk"]
    assert "it is yours" in store.load_history(thread)[-1]["text"]
    assert db.count("registration") == 2 and db.count("notification") == 1
    assert db.get_event(2)["seats_available"] == 0


def test_a_policy_refusal_is_a_normal_answer_not_a_failure(store, db):
    thread, run_id = ask(store, "23EE042", RAHUL)
    Worker(store, db, demo_providers(), worker_id="w").run_until_idle()
    assert store.get_run(run_id)["status"] == "succeeded"
    assert "62%" in store.load_history(thread)[-1]["text"]
    assert db.count("registration") == 1 and db.count("notification") == 0


def test_a_full_event_falls_back_to_the_waitlist(store, db):
    thread, run_id = ask(store, "23CS101", FULL)
    Worker(store, db, demo_providers(), worker_id="w").run_until_idle()
    assert store.get_run(run_id)["status"] == "succeeded"
    assert "waitlist" in store.load_history(thread)[-1]["text"]
    assert db.count("waitlist") == 1 and db.count("registration") == 1
    assert db.waitlist_entries(1)[0]["position"] == 1


def test_a_crash_after_the_registration_does_not_take_a_second_seat(store, db, clock):
    """The crash-and-replay test. Worker A dies the instant the seat is taken and its key stored;
    worker B claims the run when the lease expires and finishes it from the database."""
    _, run_id = ask(store, "23CS101", MEERA)
    real_once = db.once

    def once_then_die(key, tool_name, effect):
        result = real_once(key, tool_name, effect)
        if tool_name == "register_for_event":
            raise SimulatedCrash()
        return result

    db.once = once_then_die
    with pytest.raises(SimulatedCrash):
        Worker(store, db, demo_providers(), worker_id="A", lease_seconds=30).run_once()
    db.once = real_once
    assert db.count("registration") == 2                     # the seat is gone
    assert db.count("notification") == 0                     # the text never went out
    assert store.get_run(run_id)["status"] == "running"      # and the run still looks alive

    clock.advance(31)
    assert Worker(store, db, demo_providers(), worker_id="B",
                  lease_seconds=30).run_until_idle() == [(run_id, "succeeded")]
    assert db.count("registration") == 2 and db.count("notification") == 1
    assert db.get_event(2)["seats_available"] == 0
    assert store.get_run(run_id)["attempts"] == 2


def test_the_replayed_side_effect_reuses_its_stored_result(store, db, clock):
    """Not just 'no duplicate': the second attempt returns the first attempt's answer."""
    _, run_id = ask(store, "23CS101", MEERA)
    real_once = db.once

    def once_then_die(key, tool_name, effect):
        result = real_once(key, tool_name, effect)
        if tool_name == "register_for_event":
            raise SimulatedCrash()
        return result

    db.once = once_then_die
    with pytest.raises(SimulatedCrash):
        Worker(store, db, demo_providers(), worker_id="A", lease_seconds=30).run_once()
    db.once = real_once
    stored = db.conn.execute(
        "SELECT result FROM idempotency WHERE tool_name = 'register_for_event'").fetchone()["result"]
    assert stored["status"] == "registered"

    clock.advance(31)
    Worker(store, db, demo_providers(), worker_id="B", lease_seconds=30).run_until_idle()
    keys = db.conn.execute("SELECT tool_name FROM idempotency ORDER BY tool_name").fetchall()
    assert [k["tool_name"] for k in keys] == ["notify_student", "register_for_event"]


def test_asking_the_same_thing_twice_still_takes_one_seat(store, db):
    """Two separate runs, so two separate key families. The unique constraint and the notification's
    own dedupe key are what keep this to one seat and one text."""
    for _ in range(2):
        ask(store, "23CS101", MEERA)
    Worker(store, db, demo_providers(), worker_id="w").run_until_idle()
    assert db.count("registration") == 2 and db.count("notification") == 1
    assert db.get_event(2)["seats_available"] == 0


def test_every_side_effect_is_recorded_with_its_key(store, db):
    _, run_id = ask(store, "23CS101", MEERA)
    Worker(store, db, demo_providers(), worker_id="w").run_until_idle()
    calls = store.conn.execute(
        "SELECT tool_name, idempotency_key FROM tool_call ORDER BY id").fetchall()
    assert calls and all(c["idempotency_key"] for c in calls)
