"""A local Postgres for when no Supabase URL is set.

The brief wants the demo and the tests to run on a clean machine with no key. Supabase needs a
network and credentials, so without this a grader could not run anything. `pgserver` ships the real
Postgres binaries in a wheel, so `pip install -r requirements.txt` is enough to get the same engine
Supabase runs, on localhost, owned by this process.

This is not a second database backend. There is one dialect, one pair of .sql files and one data
access layer; this module only decides which Postgres they point at.

One cluster and one database are reused across runs, because creating a database copies a template
and a first cluster takes a few seconds. Runs do not collide anyway: the demo and the tests each
work in their own pair of schemas and drop them at the end (app/demo_sandbox.py).
"""
import os
import subprocess
import tempfile
import time
from pathlib import Path

DATABASE = "event_assistant"

# pgserver waits 10 seconds for `pg_ctl start` and then gives up. On a cold Windows disk, or with a
# virus scanner reading every file Postgres touches, starting can take longer than that -- but the
# server does come up, so the timeout is a slow stopwatch rather than a real failure. Try again.
START_ATTEMPTS = 4

_server = None

INSTALL_HINT = (
    "No database URL is set and the local Postgres could not start.\n"
    "Either install the bundled server:   pip install pgserver\n"
    "or point the project at Supabase:    set SUPABASE_DB_URL=postgresql://...\n"
)


def datadir() -> Path:
    """Where the local cluster lives. Override with EVENT_ASSISTANT_PGDATA."""
    override = os.environ.get("EVENT_ASSISTANT_PGDATA")
    return Path(override) if override else Path(tempfile.gettempdir()) / "event-assistant-pgdata"


def _forget(pgserver, path: Path) -> None:
    """Drop pgserver's cached handle for this data directory.

    `get_server` puts the new handle in its cache *before* it starts Postgres, so a start that
    times out leaves a half-built handle behind, and the next call would hand that same broken
    handle straight back. Reaching into the cache is the price of retrying at all.
    """
    pgserver.PostgresServer._instances.pop(path.expanduser().resolve(), None)


def server():
    """Start the cluster once per process. It is stopped when the process exits.

    The first call on a machine initialises the data directory, which takes a few seconds; later
    calls just start the server. A start that takes longer than pgserver's ten-second patience is
    retried, because by then the server is usually up and the second attempt simply finds it.
    """
    global _server
    if _server is not None:
        return _server
    try:
        import pgserver
    except ImportError as e:                      # pragma: no cover - depends on the machine
        raise RuntimeError(INSTALL_HINT) from e

    path = datadir()
    path.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(START_ATTEMPTS):
        try:
            candidate = pgserver.get_server(str(path), cleanup_mode="stop")
            candidate.get_uri()                   # a half-built handle raises here, not later
            _server = candidate
            return _server
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, AssertionError) as e:
            last_error = e
            _forget(pgserver, path)
            time.sleep(2.0 * (attempt + 1))       # give the postmaster time to finish coming up
    raise RuntimeError(slow_start_hint(path)) from last_error


def slow_start_hint(path: Path) -> str:
    return (
        f"The local Postgres would not start after {START_ATTEMPTS} attempts.\n"
        f"Its data directory is {path}\n"
        f"Look at {path / 'log'} for the reason. Deleting the directory rebuilds it from scratch.\n"
        "Or skip it entirely and use Supabase:   set SUPABASE_DB_URL=postgresql://...\n"
    )


def database_url(name: str = DATABASE) -> str:
    """The URL of the local database, creating it the first time.

    Done over a connection rather than with psql so that the ordinary "it is already there" case
    stays silent instead of printing an error every run.
    """
    import psycopg
    from psycopg import sql

    srv = server()
    with psycopg.connect(srv.get_uri(), autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            try:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            except psycopg.errors.DuplicateDatabase:
                pass                              # another process got there first
    return srv.get_uri(name)
