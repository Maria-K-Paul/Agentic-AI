"""Starting the local Postgres, including when it is slow.

This is here because it actually bit: `pgserver` waits ten seconds for `pg_ctl start` and then
raises, and on a cold Windows disk the server takes longer than that -- but it does come up, so the
timeout is a slow stopwatch rather than a real failure. Worse, `get_server` caches the new handle
*before* it starts Postgres, so a plain retry is handed back the same half-built handle.

These tests run against a stand-in for pgserver, so they neither start a server nor take ten
seconds to find out what happens when one is slow.
"""
import subprocess
import sys
import types
from pathlib import Path

import pytest

from app import localpg


class Started:
    """A handle whose server came up."""

    def get_uri(self, database=None):
        return "postgresql://postgres:@127.0.0.1:1/postgres"


class HalfBuilt:
    """What pgserver leaves in its cache when the start times out: asking it anything asserts."""

    def get_uri(self, database=None):
        raise AssertionError("postmaster info was never set")


class FakePgserver(types.ModuleType):
    """pgserver's contract, including the part that caches before it starts."""

    def __init__(self, slow_starts: int):
        super().__init__("pgserver")
        self.slow_starts, self.starts = slow_starts, 0
        self.PostgresServer = types.SimpleNamespace(_instances={})

    def get_server(self, pgdata, cleanup_mode=None):
        key = Path(pgdata).expanduser().resolve()
        if key in self.PostgresServer._instances:
            return self.PostgresServer._instances[key]
        self.starts += 1
        if self.starts <= self.slow_starts:
            self.PostgresServer._instances[key] = HalfBuilt()      # cached, and only then started
            raise subprocess.TimeoutExpired("pg_ctl", 10)
        self.PostgresServer._instances[key] = Started()
        return self.PostgresServer._instances[key]


@pytest.fixture
def start(monkeypatch, tmp_path):
    """Install a stand-in pgserver and hand back a way to say how slow it is."""
    monkeypatch.setattr(localpg, "_server", None)
    monkeypatch.setenv("EVENT_ASSISTANT_PGDATA", str(tmp_path / "pgdata"))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    def install(slow_starts: int) -> FakePgserver:
        fake = FakePgserver(slow_starts)
        monkeypatch.setitem(sys.modules, "pgserver", fake)
        return fake

    return install


def test_a_server_that_starts_first_time_is_not_retried(start):
    fake = start(slow_starts=0)
    assert localpg.server() is not None
    assert fake.starts == 1


def test_a_slow_start_is_retried_rather_than_reported_as_a_failure(start):
    """The whole point: ten seconds of patience is not the same as a broken server."""
    fake = start(slow_starts=1)
    assert localpg.server().get_uri().startswith("postgresql://")
    assert fake.starts == 2


def test_the_retry_does_not_reuse_the_handle_the_failed_start_cached(start):
    """Without evicting pgserver's cache, every retry gets the same half-built handle back."""
    fake = start(slow_starts=2)
    localpg.server()
    assert fake.starts == 3
    assert isinstance(fake.PostgresServer._instances[Path(localpg.datadir()).resolve()], Started)


def test_a_server_that_never_starts_says_where_to_look(start):
    fake = start(slow_starts=localpg.START_ATTEMPTS)
    with pytest.raises(RuntimeError) as caught:
        localpg.server()
    message = str(caught.value)
    assert str(localpg.datadir()) in message          # which directory to inspect or delete
    assert "SUPABASE_DB_URL" in message               # and the way out that needs no local server
    assert fake.starts == localpg.START_ATTEMPTS
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)


def test_the_started_server_is_reused_within_a_process(start):
    fake = start(slow_starts=0)
    assert localpg.server() is localpg.server()
    assert fake.starts == 1


def test_the_data_directory_can_be_moved_with_an_environment_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("EVENT_ASSISTANT_PGDATA", str(tmp_path / "somewhere-else"))
    assert localpg.datadir() == tmp_path / "somewhere-else"
    monkeypatch.delenv("EVENT_ASSISTANT_PGDATA")
    assert localpg.datadir().name == "event-assistant-pgdata"
