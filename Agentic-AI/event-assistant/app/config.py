"""Where the two databases are, and which models the three agents talk to.

The project wants TWO databases. A Supabase project is a single Postgres database, so by default
the two are two schemas, `agent` and `events`, reached over two separate connections that are each
pinned to their own search_path. Set AGENT_DB_URL and EVENTS_DB_URL to two different projects and
they become two Postgres databases; no code changes, because no code names the other schema.

Resolution order, first match wins:

    1. AGENT_DB_URL and EVENTS_DB_URL          two URLs, one per database
    2. SUPABASE_DB_URL (or DATABASE_URL)       one Supabase project, two schemas
    3. nothing set                             a local Postgres we start ourselves (app/localpg.py)

URLs come from the real environment or from .env, which is git-ignored and not in the submitted
zip. No credential is ever written into a source file.
"""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def load_env_file(path: Path = ENV_FILE) -> None:
    """Read KEY=VALUE lines from .env, without adding a dependency for it.

    Anything already in the real environment wins, so an export on the command line still overrides
    the file. Called on import, because forgetting it is a confusing way to connect to the wrong
    database.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()


def using_supabase() -> bool:
    """True when the operator pointed the project at a real Postgres, Supabase or otherwise."""
    return bool((os.environ.get("AGENT_DB_URL") and os.environ.get("EVENTS_DB_URL"))
                or os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL"))


def database_urls() -> tuple[str, str]:
    """Return (agent_url, events_url), starting a local Postgres if nothing is configured."""
    agent, events = os.environ.get("AGENT_DB_URL"), os.environ.get("EVENTS_DB_URL")
    if agent and events:
        return agent, events
    configured = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if configured:
        return configured, configured
    from app import localpg

    url = localpg.database_url()
    return url, url


def open_stores(migrate: bool = True):
    """Open both databases and make sure their schemas are there."""
    from app.events_db import EventsDb
    from app.memory import RunStore

    agent_url, events_url = database_urls()
    store, db = RunStore(agent_url), EventsDb(events_url)
    if migrate:
        store.migrate()
        db.migrate()
    return store, db


def make_providers(mock: bool, slow: float = 0.0) -> dict:
    """One provider per agent. With Gemini all three share one client; each keeps its own prompt
    and its own tools, which is what makes them different agents."""
    if mock:
        from app.providers import demo_providers

        return demo_providers(slow)
    from app.providers import GeminiProvider

    gemini = GeminiProvider(GEMINI_MODEL)
    return {"supervisor": gemini, "programme": gemini, "desk": gemini}
