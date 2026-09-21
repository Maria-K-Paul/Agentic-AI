"""The agent database: conversations, runs and the job queue.

This is the assistant's own memory. It holds no events, no students and no seats, and the
connection it opens cannot see those tables at all.

The queue is a table, claimed with `SELECT ... FOR UPDATE SKIP LOCKED`. That is the part Postgres
does better than a file-based database: a claim takes a row lock on exactly one run and every other
worker steps over it instead of queueing behind it, so adding workers adds throughput.
"""
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.db import connect, render_schema, run_script, transaction

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schema" / "agent.sql"
SCHEMA = "agent"
TERMINAL = ("succeeded", "failed", "cancelled", "dead")


@dataclass(frozen=True)
class Claimed:
    run_id: str
    thread_id: str
    attempts: int


class RunStore:
    def __init__(self, url: str, clock: Callable[[], float] = time.time, schema: str = SCHEMA):
        self.schema = schema
        self.conn = connect(url, schema)
        self.clock = clock

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        run_script(self.conn, render_schema(SCHEMA_FILE.read_text(), SCHEMA, self.schema))

    def transaction(self):
        return transaction(self.conn)

    # ================================================================== threads and transcripts

    def create_thread(self, student_id: str) -> str:
        thread_id = str(uuid.uuid4())
        self.conn.execute("INSERT INTO thread (id, student_id) VALUES (%s, %s)", (thread_id, student_id))
        return thread_id

    def get_thread(self, thread_id: str) -> dict | None:
        return self.conn.execute("SELECT * FROM thread WHERE id = %s", (thread_id,)).fetchone()

    def append_message(self, thread_id: str, role: str, text: str) -> int:
        """Append one turn. The seq comes from the table, and UNIQUE (thread_id, seq) means a race
        between two appends fails loudly rather than quietly overwriting a turn."""
        return self.conn.execute(
            "INSERT INTO message (thread_id, seq, role, text)"
            " VALUES (%s, (SELECT COALESCE(MAX(seq), 0) + 1 FROM message WHERE thread_id = %s), %s, %s)"
            " RETURNING seq", (thread_id, thread_id, role, text)).fetchone()["seq"]

    def load_history(self, thread_id: str) -> list[dict]:
        return self.conn.execute("SELECT seq, role, text FROM message WHERE thread_id = %s ORDER BY seq",
                                 (thread_id,)).fetchall()

    # ================================================================== what a run did

    def record_model_step(self, run_id: str, seq: int, tokens_in: int, tokens_out: int,
                          text: str | None, tool_calls: list[dict]) -> int:
        with self.transaction() as c:
            step_id = c.execute(
                "INSERT INTO run_step (run_id, seq, kind, tokens_in, tokens_out, text, tool_calls)"
                " VALUES (%s, %s, 'model', %s, %s, %s, %s) RETURNING id",
                (run_id, seq, tokens_in, tokens_out, text, json.dumps(tool_calls))).fetchone()["id"]
            c.execute("UPDATE run SET tokens_in = tokens_in + %s, tokens_out = tokens_out + %s WHERE id = %s",
                      (tokens_in, tokens_out, run_id))
            return step_id

    def record_tool_call(self, run_id: str, seq: int, name: str, args: dict, result: dict,
                         ok: bool, latency_ms: int, idempotency_key: str | None = None) -> int:
        with self.transaction() as c:
            step_id = c.execute("INSERT INTO run_step (run_id, seq, kind) VALUES (%s, %s, 'tool') RETURNING id",
                                (run_id, seq)).fetchone()["id"]
            c.execute("INSERT INTO tool_call (run_step_id, tool_name, args, result, ok, latency_ms, idempotency_key)"
                      " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                      (step_id, name, json.dumps(args, default=str), json.dumps(result, default=str),
                       ok, latency_ms, idempotency_key))
            return step_id

    def load_steps(self, run_id: str) -> list[dict]:
        """Every recorded step of a run, in order. Used to resume after a crash.

        jsonb columns come back as Python objects, so there is nothing to decode here.
        """
        return self.conn.execute(
            """SELECT s.seq, s.kind, s.text, s.tool_calls, t.tool_name, t.args, t.result, t.ok
                 FROM run_step s LEFT JOIN tool_call t ON t.run_step_id = s.id
                WHERE s.run_id = %s ORDER BY s.seq""", (run_id,)).fetchall()

    def get_run(self, run_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM run WHERE id = %s", (run_id,)).fetchone()
        return {**row, "steps": self.load_steps(run_id)} if row else None

    # ================================================================== the queue

    def enqueue(self, thread_id: str, text: str, model: str, max_attempts: int = 3) -> str:
        """Save the student's message and a queued run for it, together. Return the run id.

        One transaction, so there is never a run with no question or a question with no run.
        """
        run_id = str(uuid.uuid4())
        with self.transaction() as c:
            self.append_message(thread_id, "user", text)
            c.execute("INSERT INTO run (id, thread_id, status, model, max_attempts, available_at)"
                      " VALUES (%s, %s, 'queued', %s, %s, %s)", (run_id, thread_id, model, max_attempts,
                                                                 self.clock()))
        return run_id

    def claim_next(self, worker_id: str, lease_seconds: float) -> Claimed | None:
        """Atomically take the oldest claimable run: queued and available now.

        The inner SELECT locks one row and SKIP LOCKED makes every other worker walk past it, so N
        workers claim N different runs with no retries and no gaps. The run becomes running, leased
        to this worker until now + lease_seconds, with attempts + 1.
        """
        now = self.clock()
        row = self.conn.execute(
            """UPDATE run SET status = 'running', lease_owner = %s, lease_until = %s,
                              attempts = attempts + 1, started_at = COALESCE(started_at, now())
                WHERE id = (SELECT id FROM run WHERE status = 'queued' AND available_at <= %s
                             ORDER BY available_at, created_at
                             FOR UPDATE SKIP LOCKED LIMIT 1)
             RETURNING id, thread_id, attempts""",
            (worker_id, now + lease_seconds, now)).fetchone()
        return Claimed(row["id"], row["thread_id"], row["attempts"]) if row else None

    def heartbeat(self, run_id: str, worker_id: str, lease_seconds: float) -> bool:
        """Extend the lease. False means this worker no longer owns the run: stop working on it."""
        return self.conn.execute(
            "UPDATE run SET lease_until = %s WHERE id = %s AND status = 'running' AND lease_owner = %s",
            (self.clock() + lease_seconds, run_id, worker_id)).rowcount == 1

    def reap_expired(self) -> list[str]:
        """Runs whose worker died: still running, lease expired. Requeue them, or dead-letter the ones
        that have used all their attempts. Return the ids touched.

        Nothing here asks whether the worker is really dead, because nothing can know that. An expired
        lease is the definition of dead, which is why the worker must keep its heartbeat up.
        """
        now = self.clock()
        with self.transaction() as c:
            rows = c.execute("SELECT id, attempts, max_attempts FROM run"
                             " WHERE status = 'running' AND lease_until < %s"
                             " FOR UPDATE SKIP LOCKED", (now,)).fetchall()
            for r in rows:
                if r["attempts"] >= r["max_attempts"]:
                    c.execute("UPDATE run SET status = 'dead', error_code = 'lease_expired',"
                              " lease_owner = NULL, lease_until = NULL, finished_at = now()"
                              " WHERE id = %s", (r["id"],))
                else:
                    c.execute("UPDATE run SET status = 'queued', error_code = 'lease_expired',"
                              " lease_owner = NULL, lease_until = NULL, available_at = %s"
                              " WHERE id = %s", (now, r["id"]))
            return [r["id"] for r in rows]

    def complete(self, run_id: str, worker_id: str, reply: str) -> bool:
        """Save the answer and mark the run succeeded, together, only if this worker still owns it.

        The ownership check is what stops a worker that was declared dead, but is in fact still
        running, from writing a second answer onto a run another worker has taken over.
        """
        with self.transaction() as c:
            row = c.execute("SELECT thread_id FROM run WHERE id = %s AND status = 'running'"
                            " AND lease_owner = %s", (run_id, worker_id)).fetchone()
            if row is None:
                return False
            self.append_message(row["thread_id"], "model", reply)
            c.execute("UPDATE run SET status = 'succeeded', lease_owner = NULL, lease_until = NULL,"
                      " error_code = NULL, finished_at = now() WHERE id = %s", (run_id,))
            return True

    # ================================================================== cancel

    def request_cancel(self, run_id: str) -> str | None:
        """A queued run is cancelled at once. A running run is flagged; its worker stops after the
        current step. Finished runs are left alone. Returns the status after the call, or None."""
        with self.transaction() as c:
            row = c.execute("SELECT status FROM run WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "queued":
                c.execute("UPDATE run SET status = 'cancelled', finished_at = now() WHERE id = %s", (run_id,))
                return "cancelled"
            if row["status"] == "running":
                c.execute("UPDATE run SET cancel_requested = true WHERE id = %s", (run_id,))
            return row["status"]

    def cancel_requested(self, run_id: str) -> bool:
        row = self.conn.execute("SELECT cancel_requested FROM run WHERE id = %s", (run_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def mark_cancelled(self, run_id: str, worker_id: str) -> bool:
        return self.conn.execute(
            "UPDATE run SET status = 'cancelled', lease_owner = NULL, lease_until = NULL,"
            " finished_at = now() WHERE id = %s AND status = 'running' AND lease_owner = %s",
            (run_id, worker_id)).rowcount == 1

    # ================================================================== retry and dead-letter

    def fail_attempt(self, run_id: str, worker_id: str, error_code: str, retryable: bool,
                     backoff_seconds: float = 2.0) -> str | None:
        """An attempt failed. Not retryable: 'failed'. Retryable with attempts left: back to 'queued',
        available after backoff_seconds * 2 ** (attempts - 1). Retryable with none left: 'dead'.

        Backoff doubles because the usual retryable failure is a provider that is rate limiting us,
        and hammering it on a fixed interval is how a queue turns one outage into a longer one.
        """
        with self.transaction() as c:
            row = c.execute("SELECT attempts, max_attempts FROM run"
                            " WHERE id = %s AND status = 'running' AND lease_owner = %s",
                            (run_id, worker_id)).fetchone()
            if row is None:
                return None
            if retryable and row["attempts"] < row["max_attempts"]:
                delay = backoff_seconds * 2 ** (row["attempts"] - 1)
                c.execute("UPDATE run SET status = 'queued', error_code = %s, lease_owner = NULL,"
                          " lease_until = NULL, available_at = %s WHERE id = %s",
                          (error_code, self.clock() + delay, run_id))
                return "queued"
            status = "dead" if retryable else "failed"
            c.execute("UPDATE run SET status = %s, error_code = %s, lease_owner = NULL, lease_until = NULL,"
                      " finished_at = now() WHERE id = %s", (status, error_code, run_id))
            return status
