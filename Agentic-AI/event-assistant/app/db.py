"""One way to open Postgres, used by both databases.

Everything here is plain Postgres, which is what Supabase runs. There is no second dialect and no
SQLite: the statements that go to Supabase in production are the statements the tests run.

Each connection is pinned to ONE schema with `search_path`. That is not decoration. A query in the
agent's store that named an events table would fail with `relation does not exist`, which is the
boundary the brief asks for, enforced by the server rather than by good intentions.
"""
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

# A statement that cannot get its lock inside this window has hit a real problem, not a busy moment.
LOCK_TIMEOUT_MS = 5000
STATEMENT_TIMEOUT_MS = 15000


def connect(url: str, schema: str) -> psycopg.Connection:
    """Autocommit connection pinned to one schema; transactions are explicit.

    The three settings below are session state, so on Supabase this needs a direct connection or the
    SESSION-mode pooler (port 5432), not the transaction-mode pooler (port 6543). Transaction mode
    hands a different backend to each transaction, which would drop the search_path that keeps the
    two databases apart. The README says the same thing where you paste the URL.

    `prepare_threshold=None` stops psycopg caching server-side prepared statements, which any
    connection pooler in front of Postgres is happier without.
    """
    conn = psycopg.connect(url, autocommit=True, row_factory=dict_row, prepare_threshold=None)
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    conn.execute(f"SET lock_timeout = {LOCK_TIMEOUT_MS}")
    conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return conn


@contextmanager
def transaction(conn: psycopg.Connection):
    """A transaction, or a savepoint inside one that is already open.

    psycopg gives us the nesting for free, so a helper that opens a transaction can be called from
    inside another one without either of them having to know.
    """
    with conn.transaction():
        yield conn


def run_script(conn: psycopg.Connection, statements: str) -> None:
    """Apply a schema file. Postgres runs a multi-statement string in one implicit transaction."""
    conn.execute(statements)


def render_schema(statements: str, default_schema: str, schema: str) -> str:
    """Point a schema file at a different schema.

    The .sql files name their schema on exactly two lines, `CREATE SCHEMA ...` and `SET search_path
    ...`, and everything after that is unqualified. That keeps the files paste-able straight into
    the Supabase SQL editor while still letting the demo and the tests run them into a schema of
    their own, so neither one writes over data that matters.
    """
    if schema == default_schema:
        return statements
    if not schema.replace("_", "").isalnum():
        raise ValueError(f"unsafe schema name: {schema!r}")
    header = (f"CREATE SCHEMA IF NOT EXISTS {default_schema};",
              f"SET search_path TO {default_schema};")
    for line in header:
        if line not in statements:
            raise ValueError(f"schema file does not contain the expected line: {line!r}")
        statements = statements.replace(line, line.replace(default_schema, schema))
    return statements


def reset_identity(conn: psycopg.Connection, table: str, column: str = "id") -> None:
    """Move an identity sequence past rows that were inserted with explicit ids.

    Seed data names its own ids so the fixtures and the README can talk about `event 2`. Without
    this the next generated id would be 1 and collide.
    """
    conn.execute(
        sql.SQL(
            "SELECT setval(pg_get_serial_sequence({t}, {c}),"
            "              COALESCE((SELECT max({col}) FROM {tbl}), 1),"
            "              (SELECT max({col}) FROM {tbl}) IS NOT NULL)"
        ).format(t=sql.Literal(table), c=sql.Literal(column),
                 col=sql.Identifier(column), tbl=sql.Identifier(table))
    )
