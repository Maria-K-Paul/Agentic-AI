"""Create both schemas on Supabase and seed the events data.

    python -m scripts.setup_supabase            # create what is missing, seed if empty
    python -m scripts.setup_supabase --check    # connect and report, change nothing
    python -m scripts.setup_supabase --reset    # drop both schemas first (asks first)

Reads SUPABASE_DB_URL (or AGENT_DB_URL and EVENTS_DB_URL) from the environment or from .env. You can
do the same thing by pasting schema/agent.sql and schema/events.sql into the Supabase SQL editor;
this script only saves the trip, and then seeds, which the .sql files do not do.
"""
import argparse
import sys

from app.config import database_urls, using_supabase
from app.events_db import SCHEMA as EVENTS_SCHEMA
from app.events_db import EventsDb
from app.memory import SCHEMA as AGENT_SCHEMA
from app.memory import RunStore
from scripts._term import CYAN, DIM, GREEN, RED, RESET, redact


def describe(conn, schema: str) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name",
        (schema,)).fetchall()
    return [r["table_name"] for r in rows]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true", help="connect and report, change nothing")
    p.add_argument("--reset", action="store_true", help="drop both schemas and build them again")
    a = p.parse_args()

    if not using_supabase():
        print(f"{RED}No database URL set.{RESET}")
        print(f"{DIM}Put SUPABASE_DB_URL=postgresql://... in .env (see .env.example), or export it.{RESET}")
        return 1

    agent_url, events_url = database_urls()
    print(f"{DIM}agent  -> {redact(agent_url)}  schema {AGENT_SCHEMA}{RESET}")
    print(f"{DIM}events -> {redact(events_url)}  schema {EVENTS_SCHEMA}{RESET}\n")

    if a.reset:
        answer = input(f"{RED}Drop schemas {AGENT_SCHEMA} and {EVENTS_SCHEMA} and everything in them? "
                       f"Type yes to continue: {RESET}")
        if answer.strip().lower() != "yes":
            print(f"{DIM}left alone.{RESET}")
            return 1
        from app.demo_sandbox import drop_schemas

        drop_schemas(agent_url, AGENT_SCHEMA)
        drop_schemas(events_url, EVENTS_SCHEMA)
        print(f"{GREEN}dropped.{RESET}")

    store, db = RunStore(agent_url), EventsDb(events_url)
    try:
        if not a.check:
            store.migrate()
            db.migrate()
        agent_tables, events_tables = describe(store.conn, AGENT_SCHEMA), describe(db.conn, EVENTS_SCHEMA)
        print(f"{CYAN}{AGENT_SCHEMA}{RESET}  {DIM}{', '.join(agent_tables) or '(nothing yet)'}{RESET}")
        print(f"{CYAN}{EVENTS_SCHEMA}{RESET} {DIM}{', '.join(events_tables) or '(nothing yet)'}{RESET}")
        if events_tables:
            print(f"\n{DIM}seeded: {db.count('student')} students, {db.count('event')} events, "
                  f"{db.count('policy')} policies, {db.count('registration')} registration(s){RESET}")
        # The boundary is the point of having two: prove it rather than claim it.
        try:
            store.conn.execute("SELECT 1 FROM event LIMIT 1")
            print(f"{RED}WARNING: the agent connection can see the events tables.{RESET}")
        except Exception:
            store.conn.rollback()
            print(f"{GREEN}the agent connection cannot see the events tables.{RESET}")
    finally:
        store.close()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
