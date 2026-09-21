"""Two databases, and the boundary between them.

Supabase gives a project one Postgres database, so the two are two schemas reached over two
connections that are each pinned to their own search_path. These tests are what turn that from a
naming convention into something the server enforces.
"""
import psycopg
import pytest

from app.db import render_schema
from app.events_db import SCHEMA as EVENTS_SCHEMA
from app.memory import SCHEMA as AGENT_SCHEMA


def test_the_agent_connection_cannot_see_the_events_tables(store):
    for table in ("student", "event", "registration", "waitlist", "policy", "idempotency"):
        with pytest.raises(psycopg.errors.UndefinedTable):
            store.conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        store.conn.rollback()


def test_the_events_connection_cannot_see_the_agent_tables(db):
    for table in ("thread", "message", "run", "run_step", "tool_call"):
        with pytest.raises(psycopg.errors.UndefinedTable):
            db.conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        db.conn.rollback()


def search_path(conn) -> str:
    """Postgres only quotes the identifier when it has to, so compare it unquoted."""
    return conn.execute("SHOW search_path").fetchone()["search_path"].strip('"')


def test_each_store_is_pinned_to_its_own_schema(store, db, schema_names):
    agent_schema, events_schema = schema_names
    assert search_path(store.conn) == agent_schema
    assert search_path(db.conn) == events_schema


def test_the_keys_live_next_to_the_side_effects_they_guard(db, store):
    """The key and the effect are in one database, so they commit or roll back together. A key in
    the agent database could be written while the effect it guards was rolled back."""
    assert db.count("idempotency") == 0                     # it exists in the events database
    with pytest.raises(psycopg.errors.UndefinedTable):
        store.conn.execute("SELECT 1 FROM idempotency LIMIT 1")
    store.conn.rollback()


def test_the_business_data_holds_no_conversations(db):
    tables = db.conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (db.schema,)).fetchall()
    names = {t["table_name"] for t in tables}
    assert names == {"student", "event", "policy", "registration", "waitlist",
                     "notification", "idempotency"}


def test_the_agent_memory_holds_no_business_data(store):
    tables = store.conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (store.schema,)).fetchall()
    names = {t["table_name"] for t in tables}
    assert names == {"thread", "message", "run", "run_step", "tool_call"}


def test_the_schema_files_can_be_pointed_at_another_schema():
    """How the demo and the tests get their own copy without a second set of .sql files."""
    from app.events_db import SCHEMA_FILE as EVENTS_FILE
    from app.memory import SCHEMA_FILE as AGENT_FILE

    rendered = render_schema(AGENT_FILE.read_text(), AGENT_SCHEMA, "agent_somewhere_else")
    assert "CREATE SCHEMA IF NOT EXISTS agent_somewhere_else;" in rendered
    assert "SET search_path TO agent_somewhere_else;" in rendered

    rendered = render_schema(EVENTS_FILE.read_text(), EVENTS_SCHEMA, "events_somewhere_else")
    assert "SET search_path TO events_somewhere_else;" in rendered


def test_a_schema_name_that_is_not_a_plain_identifier_is_refused():
    with pytest.raises(ValueError):
        render_schema("anything", AGENT_SCHEMA, 'agent"; DROP SCHEMA public CASCADE; --')
