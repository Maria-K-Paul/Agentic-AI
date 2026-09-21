"""Throwaway schemas, so a demo run never writes over anything that matters.

`python -m scripts.demo` is meant to be run again and again, and it ends by counting rows. Both of
those go wrong if it shares a schema with real data, and on Supabase the real data is the user's.
So each run builds its own pair of schemas, uses them, and drops them on the way out.

The tests use the same helper for the same reason.
"""
import contextlib

import psycopg
from psycopg import sql

from app.events_db import SCHEMA as EVENTS_SCHEMA
from app.events_db import EventsDb
from app.memory import SCHEMA as AGENT_SCHEMA
from app.memory import RunStore


def drop_schemas(url: str, *schemas: str) -> None:
    """Remove schemas and everything in them. Only ever called with names this module generated."""
    with psycopg.connect(url, autocommit=True) as conn:
        for name in schemas:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@contextlib.contextmanager
def sandbox(agent_url: str, events_url: str, tag: str, clock=None, keep: bool = False):
    """Open both stores in schemas named for this run, migrated and seeded. Yields (store, db)."""
    agent_schema, events_schema = f"{AGENT_SCHEMA}_{tag}", f"{EVENTS_SCHEMA}_{tag}"
    kwargs = {"clock": clock} if clock else {}
    store = RunStore(agent_url, schema=agent_schema, **kwargs)
    db = EventsDb(events_url, schema=events_schema, **kwargs)
    try:
        store.migrate()
        db.migrate()
        yield store, db
    finally:
        store.close()
        db.close()
        if not keep:
            drop_schemas(agent_url, agent_schema)
            drop_schemas(events_url, events_schema)
