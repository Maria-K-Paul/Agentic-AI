"""The whole project in one command.

    python -m scripts.demo            # scripted models: no key, no quota, same output every time
    python -m scripts.demo --real     # the same questions on Gemini (about 18 model calls)
    python -m scripts.demo --crash    # kill the run right after the registration, resume, count
    python -m scripts.demo --keep     # leave the schemas behind so you can look at the rows

With no database URL set this runs against a local Postgres it starts itself. Set SUPABASE_DB_URL
and exactly the same code runs against Supabase.

Either way the demo builds its own pair of schemas, `agent_<id>` and `events_<id>`, and drops them
on the way out. It never writes to the real `agent` and `events` schemas, so it is safe to run
against a project that has data in it, and its row counts start from a known state every time.
"""
import argparse
import uuid

from scripts._term import CYAN, DIM, GREEN, RED, RESET, print_step, redact

QUESTIONS = [
    ("23CS101", "Is the Robotics Bootcamp still open? If it is, sign me up and text me."),
    ("23EE042", "Can I register for Intro to Generative AI?"),
    ("23CS101", "I also want the Cloud Security Workshop."),
]


class Crash(BaseException):
    """Like kill -9: nothing catches it."""


def counts(db) -> str:
    return (f"registrations {db.count('registration')}   waitlist {db.count('waitlist')}"
            f"   notifications {db.count('notification')}   keys {db.count('idempotency')}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--real", action="store_true")
    p.add_argument("--crash", action="store_true")
    p.add_argument("--keep", action="store_true",
                   help="leave the demo's schemas behind so you can look at the rows")
    a = p.parse_args()

    from app.config import database_urls, make_providers, using_supabase
    from app.demo_sandbox import sandbox
    from app.worker import Worker

    agent_url, events_url = database_urls()
    tag = uuid.uuid4().hex[:6]
    with sandbox(agent_url, events_url, tag=tag, keep=a.keep) as (store, db):
        providers = make_providers(mock=not a.real)
        where = "Supabase" if using_supabase() else "local Postgres"
        print(f"{DIM}{where}: {redact(events_url)}{RESET}")
        print(f"{DIM}model: {providers['supervisor'].model}{RESET}")
        print(f"{DIM}before: {counts(db)}{RESET}\n")

        questions = QUESTIONS[:1] if a.crash else QUESTIONS
        for roll_no, text in questions:
            thread = store.create_thread(roll_no)
            run_id = store.enqueue(thread, text, providers["supervisor"].model)
            print(f"{CYAN}{roll_no}>{RESET} {text}")

            if a.crash:
                run_with_a_crash(store, db, providers, run_id)
            else:
                Worker(store, db, providers, worker_id="demo-worker", on_step=print_step).run_until_idle()

            run = store.get_run(run_id)
            colour = GREEN if run["status"] == "succeeded" else RED
            reply = store.load_history(thread)[-1]["text"] if run["status"] == "succeeded" else run["error_code"]
            print(f"{colour}assistant>{RESET} {reply}")
            print(f"{DIM}run {run_id[:8]} {run['status']} after {run['attempts']} attempt(s), "
                  f"{run['tokens_in']}+{run['tokens_out']} supervisor tokens{RESET}\n")

        print(f"after:  {counts(db)}")
        if a.crash:
            # The seed starts with one registration (Sana at the pitch day), so one new one is two.
            ok = db.count("registration") == 2 and db.count("notification") == 1
            print(f"{GREEN}PASS: one new registration, one text{RESET}" if ok
                  else f"{RED}FAIL: duplicates{RESET}")
        if a.keep:
            print(f"{DIM}kept: schemas agent_{tag} and events_{tag}{RESET}")


def run_with_a_crash(store, db, providers, run_id: str) -> None:
    """Kill worker-A the instant the registration is committed, then let worker-B finish the run.

    The crash lands in the worst place on purpose: the seat is gone and its idempotency key is
    stored, but the run knows nothing about either, so a resume that is not careful would take a
    second seat and send a second text.
    """
    import time

    from app.worker import Worker
    from scripts._term import print_step

    real_once = db.once

    def once_then_die(key, tool_name, effect):
        result = real_once(key, tool_name, effect)
        if tool_name == "register_for_event":
            raise Crash()        # the registration and its key are committed; nothing after is
        return result

    db.once = once_then_die
    try:
        Worker(store, db, providers, worker_id="worker-A", lease_seconds=60,
               on_step=print_step).run_once()
    except Crash:
        db.once = real_once
        print(f"\n  {RED}worker-A died right after writing the registration{RESET}")
        print(f"  {DIM}{counts(db)}; run is '{store.get_run(run_id)['status']}'{RESET}")
        store.clock = lambda: time.time() + 61      # pretend the lease ran out
        print(f"  {DIM}...lease expires, worker-B claims the run{RESET}\n")
    Worker(store, db, providers, worker_id="worker-B", lease_seconds=60,
           on_step=print_step).run_until_idle()


if __name__ == "__main__":
    main()
