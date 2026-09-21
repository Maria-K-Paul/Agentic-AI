"""The events database: students, events, registrations, waitlist. Every SQL statement for the
campus events office lives here, and nothing here knows that an agent exists.

The scarce resource is a seat. Two writers racing for the last one is the normal case, not the
exceptional one, so `register` does a compare-and-set on `event.version` and treats losing as an
ordinary outcome rather than an error.
"""
import json
import time
from collections.abc import Callable
from pathlib import Path

from psycopg import sql

from app.db import connect, render_schema, reset_identity, run_script, transaction

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schema" / "events.sql"
SCHEMA = "events"

# Advisory locks are just numbers, so give each use its own space and they cannot collide.
WAITLIST_LOCK_NAMESPACE = 8801
ONCE_LOCK_SEED = 8802

SEED_STUDENTS = [
    (1, "23CS101", "Meera Nair", "CSE", 88, 2),
    (2, "23EE042", "Rahul Menon", "EEE", 62, 2),
    (3, "23ME018", "Sana Qureshi", "MECH", 91, 1),
]

SEED_EVENTS = [
    (1, "Intro to Generative AI", "workshop", "Auditorium A", "2026-10-10 09:30+05:30", 40, 40),
    (2, "Robotics Bootcamp", "bootcamp", "Robotics Lab", "2026-10-11 10:00+05:30", 1, 1),
    (3, "Startup Pitch Day", "seminar", "Seminar Hall 2", "2026-10-14 14:00+05:30", 30, 30),
    (4, "Cloud Security Workshop", "workshop", "Lab C", "2026-10-17 11:00+05:30", 25, 0),
    (5, "Data Structures Marathon", "contest", "Online", "2026-10-19 18:00+05:30", 50, 50),
]

SEED_POLICIES = [("min_attendance_to_register", 75), ("max_waitlist_per_student", 2)]


class EventsDb:
    def __init__(self, url: str, clock: Callable[[], float] = time.time, schema: str = SCHEMA):
        self.schema = schema
        self.conn = connect(url, schema)
        self.clock = clock

    def close(self) -> None:
        self.conn.close()

    def transaction(self):
        return transaction(self.conn)

    # ------------------------------------------------------------------ setup

    def migrate(self) -> None:
        run_script(self.conn, render_schema(SCHEMA_FILE.read_text(), SCHEMA, self.schema))
        self.seed()

    def seed(self) -> None:
        """Put the office's starting data in place. Does nothing if students are already there."""
        if self.conn.execute("SELECT count(*) AS n FROM student").fetchone()["n"]:
            return
        with self.transaction() as c:
            c.cursor().executemany(
                "INSERT INTO student (id, roll_no, name, dept, attendance_pct, max_active_registrations)"
                " VALUES (%s, %s, %s, %s, %s, %s)", SEED_STUDENTS)
            c.cursor().executemany(
                "INSERT INTO event (id, title, category, venue, starts_at, seats_total, seats_available)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)", SEED_EVENTS)
            c.cursor().executemany("INSERT INTO policy (name, value) VALUES (%s, %s)", SEED_POLICIES)
            # Sana already holds her one allowed seat, which is why she is refused a second.
            c.execute("INSERT INTO registration (student_id, event_id, created_at) VALUES (3, 3, %s)",
                      (self.clock(),))
            c.execute("UPDATE event SET seats_available = seats_available - 1 WHERE id = 3")
            for table in ("student", "event", "registration", "waitlist", "notification"):
                reset_identity(c, table)

    # ------------------------------------------------------------------ reads

    def get_student(self, roll_no: str) -> dict | None:
        return self.conn.execute("SELECT * FROM student WHERE roll_no = %s", (roll_no,)).fetchone()

    def policy(self, name: str) -> int:
        row = self.conn.execute("SELECT value FROM policy WHERE name = %s", (name,)).fetchone()
        if row is None:
            raise LookupError(f"no policy named {name!r}")
        return row["value"]

    def active_registrations(self, student_id: int) -> list[dict]:
        return self.conn.execute(
            "SELECT r.event_id, e.title, e.starts_at FROM registration r JOIN event e ON e.id = r.event_id"
            " WHERE r.student_id = %s ORDER BY r.id", (student_id,)).fetchall()

    def waitlist_entries(self, student_id: int) -> list[dict]:
        return self.conn.execute(
            "SELECT w.event_id, e.title, w.position FROM waitlist w JOIN event e ON e.id = w.event_id"
            " WHERE w.student_id = %s ORDER BY w.id", (student_id,)).fetchall()

    def search_events(self, text: str, limit: int = 5) -> list[dict]:
        like = f"%{text.strip()}%"
        return self.conn.execute(
            "SELECT id, title, category, venue, starts_at, seats_available FROM event"
            " WHERE title ILIKE %s OR category ILIKE %s OR venue ILIKE %s ORDER BY starts_at, title LIMIT %s",
            (like, like, like, limit)).fetchall()

    def get_event(self, event_id: int) -> dict | None:
        return self.conn.execute("SELECT * FROM event WHERE id = %s", (event_id,)).fetchone()

    def count(self, table: str) -> int:
        return self.conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))).fetchone()["n"]

    # ------------------------------------------------------------------ safe writes

    def register(self, student_id: int, event_id: int) -> str:
        """Take one seat. Returns 'registered', 'already_registered' or 'no_seats'. Safe to repeat.

        The UPDATE carries its own guard: it fires only while a seat is left AND the row is still the
        version we read. Two workers racing for the last seat both run it, one changes a row and one
        changes none, and the loser is told 'no_seats' rather than being handed an exception.
        """
        with self.transaction() as c:
            if c.execute("SELECT 1 FROM registration WHERE student_id = %s AND event_id = %s",
                         (student_id, event_id)).fetchone():
                return "already_registered"                  # a repeat is a success, not a conflict
            row = c.execute("SELECT version FROM event WHERE id = %s", (event_id,)).fetchone()
            if row is None:
                return "no_seats"
            took = c.execute(
                "UPDATE event SET seats_available = seats_available - 1, version = version + 1"
                " WHERE id = %s AND seats_available > 0 AND version = %s",
                (event_id, row["version"])).rowcount
            if not took:
                return "no_seats"                            # a lost race is a normal outcome
            c.execute("INSERT INTO registration (student_id, event_id, created_at) VALUES (%s, %s, %s)",
                      (student_id, event_id, self.clock()))
            return "registered"

    def cancel_registration(self, student_id: int, event_id: int) -> str:
        """Give a seat back. Returns 'cancelled' or 'not_registered'. Safe to repeat.

        The seat goes back only when this call is the one that removed the row, so cancelling twice
        cannot hand the event a seat it never lost.
        """
        with self.transaction() as c:
            gone = c.execute("DELETE FROM registration WHERE student_id = %s AND event_id = %s",
                             (student_id, event_id)).rowcount
            if not gone:
                return "not_registered"
            c.execute("UPDATE event SET seats_available = LEAST(seats_available + 1, seats_total),"
                      " version = version + 1 WHERE id = %s", (event_id,))
            return "cancelled"

    def add_to_waitlist(self, student_id: int, event_id: int) -> tuple[str, int]:
        """Put a student in the queue for a full event. Returns (status, position). Safe to repeat.

        Positions are handed out under an advisory lock on the event, so two students joining at the
        same moment get 1 and 2 rather than both computing 1 and one of them losing to the UNIQUE.
        """
        with self.transaction() as c:
            c.execute("SELECT pg_advisory_xact_lock(%s, %s)", (WAITLIST_LOCK_NAMESPACE, event_id))
            row = c.execute("SELECT position FROM waitlist WHERE student_id = %s AND event_id = %s",
                            (student_id, event_id)).fetchone()
            if row is not None:
                return "already_waitlisted", row["position"]
            nxt = c.execute("SELECT COALESCE(max(position), 0) + 1 AS p FROM waitlist WHERE event_id = %s",
                            (event_id,)).fetchone()["p"]
            c.execute("INSERT INTO waitlist (student_id, event_id, position, created_at)"
                      " VALUES (%s, %s, %s, %s)", (student_id, event_id, nxt, self.clock()))
            return "waitlisted", nxt

    def record_notification(self, roll_no: str, message: str, dedupe_key: str) -> tuple[int, bool]:
        """Returns (notification_id, created). The same key twice is one row, and created is False."""
        row = self.conn.execute(
            "INSERT INTO notification (roll_no, message, dedupe_key, created_at) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
            (roll_no, message, dedupe_key, self.clock())).fetchone()
        if row is not None:
            return row["id"], True
        return self.conn.execute("SELECT id FROM notification WHERE dedupe_key = %s",
                                 (dedupe_key,)).fetchone()["id"], False

    def once(self, key: str, tool_name: str, effect: Callable[[], dict]) -> tuple[dict, bool]:
        """Run a side effect at most once per idempotency key; the effect and its key commit together.

        The advisory lock is what makes "at most once" true when two workers arrive together. Without
        it both would read no key, both would run the effect, and one would then fail on the primary
        key having already done the work. The lock is held until this transaction ends, so the second
        worker waits, then finds the stored result and returns it without doing anything.
        """
        with self.transaction() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))", (key, ONCE_LOCK_SEED))
            row = c.execute("SELECT result FROM idempotency WHERE key = %s", (key,)).fetchone()
            if row is not None:
                return row["result"], False
            result = effect()
            c.execute("INSERT INTO idempotency (key, tool_name, result, created_at) VALUES (%s, %s, %s, %s)",
                      (key, tool_name, json.dumps(result, default=str), self.clock()))
            return result, True
