"""The queue on its own: leases, the reaper, retries, dead letters and cancellation."""
import psycopg
import pytest

from app.providers import demo_providers
from app.worker import Worker

MEERA = "Is the Robotics Bootcamp still open? If it is, sign me up and text me."


def queued(store, text=MEERA, roll_no="23CS101", max_attempts=3):
    thread = store.create_thread(roll_no)
    return store.enqueue(thread, text, "mock", max_attempts=max_attempts)


def test_a_run_is_claimed_once_and_then_the_queue_is_empty(store):
    run_id = queued(store)
    claimed = store.claim_next("w1", 30)
    assert claimed.run_id == run_id and claimed.attempts == 1
    assert store.claim_next("w2", 30) is None
    assert store.get_run(run_id)["lease_owner"] == "w1"


def test_a_run_is_not_claimable_before_it_is_available(store, clock):
    run_id = queued(store)
    store.conn.execute("UPDATE run SET available_at = %s WHERE id = %s", (clock() + 10, run_id))
    assert store.claim_next("w", 30) is None
    clock.advance(11)
    assert store.claim_next("w", 30).run_id == run_id


def test_a_dead_workers_run_is_picked_up_by_another(store, clock):
    """An expired lease is the definition of a dead worker; nothing else can be known about it."""
    run_id = queued(store)
    store.claim_next("A", 30)
    clock.advance(31)
    assert store.reap_expired() == [run_id]
    assert store.get_run(run_id)["status"] == "queued"
    assert store.claim_next("B", 30).attempts == 2


def test_a_live_worker_keeps_its_run(store, clock):
    run_id = queued(store)
    store.claim_next("A", 30)
    clock.advance(20)
    assert store.heartbeat(run_id, "A", 30) is True
    clock.advance(20)
    assert store.reap_expired() == []              # the heartbeat pushed the lease out
    assert store.get_run(run_id)["status"] == "running"


def test_a_worker_that_lost_its_lease_cannot_finish_the_run(store, clock):
    run_id = queued(store)
    store.claim_next("A", 30)
    clock.advance(31)
    store.reap_expired()
    store.claim_next("B", 30)
    assert store.heartbeat(run_id, "A", 30) is False
    assert store.complete(run_id, "A", "an answer from a worker that was declared dead") is False
    assert store.get_run(run_id)["lease_owner"] == "B"


def test_a_run_that_keeps_dying_is_dead_lettered(store, clock):
    run_id = queued(store, max_attempts=2)
    for _ in range(2):
        store.claim_next("A", 30)
        clock.advance(31)
        store.reap_expired()
    run = store.get_run(run_id)
    assert run["status"] == "dead" and run["error_code"] == "lease_expired"
    assert store.claim_next("B", 30) is None       # a dead letter is not retried


def test_a_retryable_failure_backs_off_exponentially(store, clock):
    run_id = queued(store)
    start = clock()

    store.claim_next("A", 30)
    assert store.fail_attempt(run_id, "A", "provider_rate_limited", retryable=True) == "queued"
    assert store.get_run(run_id)["available_at"] == pytest.approx(start + 2.0)

    clock.advance(2)
    store.claim_next("A", 30)
    assert store.fail_attempt(run_id, "A", "provider_rate_limited", retryable=True) == "queued"
    assert store.get_run(run_id)["available_at"] == pytest.approx(clock() + 4.0)


def test_a_failure_that_will_never_work_is_not_retried(store):
    run_id = queued(store)
    store.claim_next("A", 30)
    assert store.fail_attempt(run_id, "A", "provider_error", retryable=False) == "failed"
    assert store.claim_next("B", 30) is None


def test_a_retryable_failure_with_no_attempts_left_is_dead_lettered(store):
    run_id = queued(store, max_attempts=1)
    store.claim_next("A", 30)
    assert store.fail_attempt(run_id, "A", "provider_rate_limited", retryable=True) == "dead"
    assert store.get_run(run_id)["status"] == "dead"


def test_cancelling_a_queued_run_stops_it_before_it_starts(store, db):
    run_id = queued(store)
    assert store.request_cancel(run_id) == "cancelled"
    assert Worker(store, db, demo_providers(), worker_id="w").run_until_idle() == []
    assert db.count("registration") == 1           # only the seed's: nothing ran


def test_cancelling_a_running_run_stops_it_between_steps(store, db):
    """A cancel never interrupts a tool half-way, so a seat is never taken without a record of it."""
    run_id = queued(store)
    seen = []

    def cancel_after_the_first_delegation(step):
        seen.append(step["kind"])
        if step["kind"] == "tool" and step.get("agent") == "supervisor":
            store.request_cancel(run_id)

    outcome = Worker(store, db, demo_providers(), worker_id="w",
                     on_step=cancel_after_the_first_delegation).run_once()
    assert outcome == (run_id, "cancelled")
    assert store.get_run(run_id)["status"] == "cancelled"
    assert db.count("registration") == 1           # it stopped before the desk was asked


def test_cancelling_a_finished_run_changes_nothing(store, db):
    run_id = queued(store)
    Worker(store, db, demo_providers(), worker_id="w").run_until_idle()
    assert store.request_cancel(run_id) == "succeeded"
    assert store.get_run(run_id)["status"] == "succeeded"


def test_cancelling_an_unknown_run_says_so(store):
    assert store.request_cancel("no-such-run") is None


def test_the_question_and_its_run_are_saved_together(store):
    run_id = queued(store)
    thread = store.get_run(run_id)["thread_id"]
    assert [m["role"] for m in store.load_history(thread)] == ["user"]


def test_the_transcript_cannot_be_rewritten(store):
    """Append-only is enforced by the database, not by everyone remembering to be careful."""
    run_id = queued(store)
    thread = store.get_run(run_id)["thread_id"]
    with pytest.raises(psycopg.errors.RaiseException):
        store.conn.execute("UPDATE message SET text = 'never happened' WHERE thread_id = %s", (thread,))
    with pytest.raises(psycopg.errors.RaiseException):
        store.conn.execute("DELETE FROM message WHERE thread_id = %s", (thread,))
    assert store.load_history(thread)[0]["text"] == MEERA
