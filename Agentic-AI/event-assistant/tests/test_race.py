"""Real threads on real connections, racing for the one thing that can run out.

Every test here would pass by luck against a single connection. They use one connection per thread
and a barrier, so both sides are inside the same moment when the clash happens.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

from app.events_db import EventsDb
from app.memory import RunStore

LAST_SEAT_EVENT = 2      # Robotics Bootcamp, one seat


def both(fn, *arg_sets):
    """Run fn once per argument set, on its own thread, all starting together."""
    barrier = threading.Barrier(len(arg_sets))

    def wrapped(args):
        barrier.wait(timeout=10)
        return fn(*args)

    with ThreadPoolExecutor(max_workers=len(arg_sets)) as pool:
        return list(pool.map(wrapped, arg_sets))


def test_two_students_racing_for_the_last_seat(db, db_urls, schema_names):
    _, events_url = db_urls
    _, events_schema = schema_names

    def take(student_id):
        own = EventsDb(events_url, schema=events_schema)
        try:
            return own.register(student_id, LAST_SEAT_EVENT)
        finally:
            own.close()

    results = both(take, (1,), (3,))
    assert sorted(results) == ["no_seats", "registered"]
    assert db.get_event(LAST_SEAT_EVENT)["seats_available"] == 0
    assert db.conn.execute("SELECT count(*) AS n FROM registration WHERE event_id = %s",
                           (LAST_SEAT_EVENT,)).fetchone()["n"] == 1


def test_the_loser_of_the_race_is_told_no_seats_not_handed_an_error(db, db_urls, schema_names):
    """A lost race is an ordinary outcome the model can explain, not an exception."""
    _, events_url = db_urls
    _, events_schema = schema_names

    def take(student_id):
        own = EventsDb(events_url, schema=events_schema)
        try:
            return own.register(student_id, LAST_SEAT_EVENT)
        finally:
            own.close()

    assert all(isinstance(r, str) for r in both(take, (1,), (3,)))


def test_two_threads_with_the_same_key_run_the_side_effect_once(db, db_urls, schema_names):
    """What `once` promises under concurrency: the advisory lock makes the second caller wait and
    then find the stored result, instead of both reading no key and both doing the work."""
    _, events_url = db_urls
    _, events_schema = schema_names
    ran: list[str] = []
    guard = threading.Lock()

    def effect():
        with guard:
            ran.append("x")
        return {"did": "it", "n": len(ran)}

    def call(tag):
        own = EventsDb(events_url, schema=events_schema)
        try:
            return own.once("shared-key", "register_for_event", effect)
        finally:
            own.close()

    results = both(call, ("a",), ("b",))
    assert len(ran) == 1, "the effect ran more than once"
    assert sorted(fresh for _, fresh in results) == [False, True]
    assert {tuple(sorted(r.items())) for r, _ in results} == {(("did", "it"), ("n", 1))}
    assert db.count("idempotency") == 1


def test_two_students_joining_a_waitlist_get_different_positions(db, db_urls, schema_names):
    _, events_url = db_urls
    _, events_schema = schema_names

    def join(student_id):
        own = EventsDb(events_url, schema=events_schema)
        try:
            return own.add_to_waitlist(student_id, 4)      # Cloud Security Workshop, full
        finally:
            own.close()

    results = both(join, (1,), (3,))
    assert sorted(position for _, position in results) == [1, 2]
    assert all(status == "waitlisted" for status, _ in results)


def test_two_workers_claim_different_runs(store, clock, db_urls, schema_names):
    """FOR UPDATE SKIP LOCKED: the second worker steps over the locked row rather than waiting."""
    agent_url, _ = db_urls
    agent_schema, _ = schema_names
    wanted = set()
    for _ in range(2):
        thread = store.create_thread("23CS101")
        wanted.add(store.enqueue(thread, "anything", "mock"))

    def claim(worker_id):
        own = RunStore(agent_url, clock=clock, schema=agent_schema)
        try:
            claimed = own.claim_next(worker_id, 30)
            return claimed.run_id if claimed else None
        finally:
            own.close()

    got = both(claim, ("A",), ("B",))
    assert None not in got, "a worker came away empty while a run was waiting"
    assert set(got) == wanted, "both workers claimed the same run"
