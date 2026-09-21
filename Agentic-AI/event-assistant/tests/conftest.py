"""Test fixtures.

The suite runs against a real Postgres, because that is what the project runs against. With no URL
set it starts a throwaway local one; set SUPABASE_DB_URL and the same tests run on Supabase.

Either way the tests work in schemas named for this run and drop them at the end, so running the
suite against a real project cannot touch the real `agent` and `events` schemas. The schemas are
built once and the tables emptied between tests, which is much faster than rebuilding them.
"""
import uuid

import pytest

from app.config import database_urls
from app.demo_sandbox import drop_schemas
from app.events_db import SCHEMA as EVENTS_SCHEMA
from app.events_db import EventsDb
from app.memory import SCHEMA as AGENT_SCHEMA
from app.memory import RunStore

TAG = f"test_{uuid.uuid4().hex[:8]}"

# Child tables first: TRUNCATE names them all in one statement, and CASCADE would otherwise be
# doing work we can just be explicit about.
AGENT_TABLES = "tool_call, run_step, run, message, thread"
EVENTS_TABLES = "idempotency, notification, waitlist, registration, policy, event, student"


class FakeClock:
    """Time the tests control. Leases expire because a test says so, not because it waited."""

    def __init__(self, start: float = 1_790_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SimulatedCrash(BaseException):
    """Like kill -9: nothing in the worker catches it."""


@pytest.fixture(scope="session")
def db_urls():
    return database_urls()


@pytest.fixture(scope="session")
def schema_names():
    return f"{AGENT_SCHEMA}_{TAG}", f"{EVENTS_SCHEMA}_{TAG}"


@pytest.fixture(scope="session")
def _stores(db_urls, schema_names):
    agent_url, events_url = db_urls
    agent_schema, events_schema = schema_names
    store = RunStore(agent_url, schema=agent_schema)
    db = EventsDb(events_url, schema=events_schema)
    store.migrate()
    db.migrate()
    yield store, db
    store.close()
    db.close()
    drop_schemas(agent_url, agent_schema)
    drop_schemas(events_url, events_schema)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(_stores, clock):
    store, _ = _stores
    store.conn.execute(f"TRUNCATE {AGENT_TABLES} RESTART IDENTITY")
    store.clock = clock
    return store


@pytest.fixture
def db(_stores, clock):
    _, db = _stores
    db.conn.execute(f"TRUNCATE {EVENTS_TABLES} RESTART IDENTITY")
    db.clock = clock
    db.seed()
    return db
