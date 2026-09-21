# Campus Events Assistant

Weekend project. A small end-to-end agent service for a campus events office, on Postgres/Supabase.

A student asks a question in plain English. A **supervisor** agent delegates to two **specialist**
agents: a programme specialist that can only look at the calendar, and a desk specialist that can
take seats, join waitlists and send texts. The run is a job on a queue, and a worker that dies
half-way through does not take the seat twice.

The scarce thing is a **seat**. The Robotics Bootcamp has exactly one.

```
student ─▶ queue (agent schema) ─▶ worker ─▶ supervisor ──ask_programme──▶ programme agent ─▶ search_events, get_event
                                                        │
                                                        └─ask_desk───────▶ desk agent ─────▶ get_student, check_eligibility,
                                                                                             register_for_event*, join_waitlist*,
                                                                                             cancel_registration*, notify_student*

                                                                     * side effects: run once per idempotency key
```

## Run it (no API key needed)

```bash
python -m venv .venv && .venv\Scripts\activate     # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

```bash
python -m scripts.demo
```

```bash
python -m scripts.demo --crash
```

```bash
python -m pytest
```

- `scripts.demo` — three questions, scripted models, every step printed.
- `scripts.demo --crash` — the worker dies right after the registration; a second worker finishes it
  and the run ends `PASS`.
- `pytest` — 76 tests, about ten seconds.

With no database URL set, all three start a local Postgres of their own (see
[Databases](#the-two-databases)). Nothing needs a key, a network or a running server.

**First run is slower.** Initialising the local Postgres data directory takes five to ten seconds,
once per machine. Later runs take about a second to start.

If the disk is busy, Postgres can take longer to start than `pgserver` is willing to wait, so
`app/localpg.py` retries rather than reporting a failure: the server is usually already up by
then and the next attempt simply finds it. If it really cannot start, the error names the data
directory to look in or delete. `tests/test_localpg.py` covers both paths.

## Run it on Supabase

1. Create a project at [supabase.com](https://supabase.com).
2. Project Settings → Database → Connection string → URI, and pick the **Session pooler** (port
   5432) or a direct connection. Not the transaction pooler on 6543 — see
   [Why session mode](#why-session-mode-and-not-the-transaction-pooler).
3. `cp .env.example .env` and put the URL in `SUPABASE_DB_URL`. `.env` is git-ignored and is not in
   the submitted zip.
4. Create the schemas and seed the events data:

```bash
python -m scripts.setup_supabase
```

That is the same as pasting `schema/agent.sql` and `schema/events.sql` into the Supabase SQL editor,
except that it also seeds and then checks that the boundary between the two databases holds. Use
`--check` to connect and report without changing anything.

Everything else then runs unchanged against Supabase:

```bash
python -m scripts.demo
```

```bash
python -m pytest
```

Both work in schemas of their own and drop them at the end, so neither writes over the real `agent`
and `events` schemas. You can run them against a live project.

## Two terminals

```bash
python -m scripts.worker --mock --slow 4
```

```bash
python -m scripts.ask --student 23CS101 "Is the Robotics Bootcamp still open? Sign me up."
```

```bash
python -m scripts.cancel <run-id>
```

`scripts.ask` prints the run id. `scripts.cancel --list` shows what can still be cancelled. Start two
workers and they take different runs.

## With Gemini

```bash
python -m scripts.demo --real
```

Needs `GEMINI_API_KEY`. One question costs about six model calls across three agents, so the free
tier runs out quickly. Everything except a final check runs on the scripted models.

**Not yet run against real Gemini.** I have no key, so the live call is the one thing in this
project I have not seen work. What is tested without a key is the translation either side of it:
`tests/test_providers.py` covers `GeminiProvider._to_gemini`, including the rule that trips people
up — two function responses to one model turn have to travel together or the API rejects the
conversation — and that a turn Gemini gave us is sent back unchanged so its thought signatures
survive. Everything else in this README I ran.

## The domain

| | |
|---|---|
| Read-only tools | `search_events`, `get_event`, `get_student`, `check_eligibility` |
| Side-effect tools | `register_for_event`, `join_waitlist`, `cancel_registration`, `notify_student` |
| The thing that can clash | a seat: `event.seats_available` |
| The rule that lives in data | `policy.min_attendance_to_register`, `policy.max_waitlist_per_student` |
| The message to send | `notify_student`, deduplicated per student per day |

### Seed data

| Student | Attendance | Own limit | What happens |
|---|---|---|---|
| 23CS101 Meera Nair | 88% | 2 | Can register |
| 23EE042 Rahul Menon | 62% | 2 | Refused: below the 75% the policy table requires |
| 23ME018 Sana Qureshi | 91% | 1 | Refused: already holds her one registration |

| # | Event | Seats |
|---|---|---|
| 1 | Intro to Generative AI | 40 of 40 |
| 2 | **Robotics Bootcamp** | **1 of 1** — the one that can clash |
| 3 | Startup Pitch Day | 29 of 30 (Sana holds one) |
| 4 | Cloud Security Workshop | **0 of 25** — full, so the waitlist path runs |
| 5 | Data Structures Marathon | 50 of 50 |

The three demo questions walk one path each: a successful registration, a refusal that comes from
the policy table, and a full event falling back to the waitlist.

## The two databases

The brief asks for two databases: the domain's data in one, the agent's memory and queue in the
other. A Supabase project is a **single Postgres database**, so the two are two schemas reached over
two separate connections:

| | Schema | Holds |
|---|---|---|
| Events | `events` | students, events, registrations, waitlist, notifications, policy, idempotency keys |
| Agent | `agent` | threads, messages, runs, run steps, tool calls — the queue |

This is a real boundary, not a naming convention. Each connection is pinned to its own schema with
`SET search_path`, and nothing in either data access layer ever names the other schema. A query in
the agent's store that reached for `event` fails with `relation does not exist`, from the server.
`tests/test_databases.py` asserts exactly that, in both directions.

Set `AGENT_DB_URL` and `EVENTS_DB_URL` to two different Supabase projects and they become two
Postgres databases, with no code change.

### Why session mode, and not the transaction pooler

The connection sets `search_path`, `lock_timeout` and `statement_timeout` once, at connect time.
That is session state. Supabase's transaction-mode pooler (port 6543) hands each transaction a
different backend, so the `search_path` would not be there for the next query, and the boundary
above would quietly stop existing. Use the session pooler (5432) or a direct connection.

### Where SQLite would have gone

The brief says two **SQLite** databases; this project uses Supabase throughout, which was the
requirement I was given. The cost is that requirement 7 — "runs on a clean machine with no key" —
is not free any more, because Supabase needs a network and a credential.

So `app/localpg.py` starts a real Postgres from a pip wheel when no URL is set. That is not a second
backend: there is one dialect, one pair of `.sql` files and one data access layer, and the only
thing that changes is which Postgres they point at. The statements the tests run are the statements
that go to Supabase.

## What Postgres bought

Porting from the SQLite sample was not a translation exercise; three things got better.

| | SQLite | Here |
|---|---|---|
| Claiming a job | `BEGIN IMMEDIATE`, one writer at a time | `FOR UPDATE SKIP LOCKED`: workers step over each other's rows, so more workers means more throughput |
| Exactly-once under concurrency | serialised by the write lock | `pg_advisory_xact_lock` on the key inside `EventsDb.once`, so the second caller waits and then finds the stored result |
| Handing out waitlist positions | serialised by the write lock | an advisory lock on the event, so two people joining together get 1 and 2 |

`tests/test_race.py` drives all three with real threads on separate connections and a barrier.

## How each requirement is met

| # | Requirement | Where |
|---|---|---|
| 1 | Two databases, seeded | `schema/agent.sql`, `schema/events.sql`, `EventsDb.seed`, `tests/test_databases.py` |
| 2 | Five or more tools, 2+ read-only, 2+ with side effects | `app/tools/event_tools.py` — four of each |
| 3 | A rule in data, enforced even if the model skips the check | `policy` table; `register_for_event` calls `check_eligibility` itself |
| 4 | Queue, worker, lease, dead worker picked up | `RunStore.claim_next`/`heartbeat`/`reap_expired`, `app/worker.py` |
| 5 | Idempotency keys; a side effect safe to repeat on its own | `EventsDb.once`; `register`, `cancel_registration` and `add_to_waitlist` are each safe alone |
| 6 | Supervisor plus a specialist with no write tools | `app/agents.py`; `ProgrammeTools.SIDE_EFFECTS` is empty |
| 7 | Proof with no API key | `scripts/demo.py`, `scripts/demo.py --crash`, 76 tests |
| 8 | Higher grade | cancel from a second terminal, retry with backoff and dead-lettering, a race test with threads — all three |

### Requirement 8, in detail

- **Cancel from a second terminal** — `scripts/cancel.py`. It only sets a flag; the worker notices it
  between steps, never inside a tool, so a seat is never taken without a record of it.
- **Retry with backoff and dead-lettering** — `RunStore.fail_attempt`. Retryable failures go back to
  `queued` with `2 ** (attempts - 1)` seconds of backoff; when the attempts run out the run becomes
  `dead` and is never claimed again. `tests/test_queue.py`.
- **A race test with threads** — `tests/test_race.py`, five tests on separate connections.
- **A real Gemini run** — not done, see above. The adapter around it is tested; the call is not.

## Design notes

**Tool descriptions are prompts.** Every tool says when to use it, when not to, and what it changes.
`join_waitlist` says it is not a registration and guarantees nothing. `notify_student` says never to
use it to answer a question. A test asserts every description is at least 200 characters and
documents its return value, because an under-described tool is a bug that only shows up in
production.

**Rules live in data.** `check_eligibility` reads `policy`, and `tests/test_tools.py` changes the row
and watches the answer change. `register_for_event` re-checks the policy itself, so a model that
never calls `check_eligibility` gains nothing by skipping it.

**Least privilege is structural.** The programme agent is constructed with `ProgrammeTools`, which
has no write tools, so no prompt can make it take a seat. The desk agent is constructed with a roll
number and none of its tools has a parameter for one, so it cannot act for another student. Both are
asserted by tests rather than promised in a prompt.

**A lost race is an outcome, not an error.** `register` returns `no_seats` rather than raising, so
the model can explain it. Repeats are outcomes too: `already_registered`, `already_waitlisted`,
`not_registered`.

**Keys are passed down.** A delegation's key is the parent of every key inside it, so replaying a
delegation replays its side effects instead of redoing them. `run_tool` in `app/agents.py`.

**Two kinds of safety, on purpose.** The idempotency key makes a repeat free. The database
constraint makes a repeat *safe* even when the key is different — a second run of the same question
has a different run id, so different keys, and `UNIQUE (student_id, event_id)` is what stops the
second seat. Both are tested.

## Known limits

- **The live Gemini call is unverified.** No key. The adapter on either side of it is tested
  (`tests/test_providers.py`); everything else here was run end to end.
- **A specialist's inner steps are not stored**; only the delegation and its answer are. After a
  crash the specialist runs again and keys keep its side effects single. Storing them is
  checkpointing.
- **Keys only match if the model repeats the same call.** The scripted models always do, and real
  models usually do at temperature 0. The registration, the waitlist place and the text are each
  also safe to repeat on their own, which covers the rest.
- **The waitlist does not promote anyone.** Cancelling a registration returns the seat to the pool;
  it does not offer it to whoever is first in the queue. That wants a background job, and it is the
  obvious next piece of work.
- **No approval step before a side effect**, no guardrails, no metrics, no MCP.
- **`pgserver` has no wheel for every platform.** If `pip install -r requirements.txt` cannot build
  it, set `SUPABASE_DB_URL` and everything works; only the keyless local fallback is lost.
- **Row Level Security is not set up.** The service connects as the database owner, which is right
  for a worker but means the schema is not safe to expose through Supabase's public API.

## Layout

```
app/
  config.py        which databases and which models; loads .env
  db.py            one way to open Postgres, pinned to one schema
  localpg.py       a local Postgres when no URL is set
  memory.py        the agent database: threads, messages, runs, the queue
  events_db.py     the events database: every statement for the office
  demo_sandbox.py  throwaway schemas for the demo and the tests
  idempotency.py   stable fingerprints for side effects
  providers.py     Gemini, plus the scripted models the demo and tests run on
  agents.py        supervisor and the two specialists
  runner.py        one run, step by step, resumable
  worker.py        claim, execute, record
  tools/
    dispatch.py    a model's tool call to a Python call (given, unchanged)
    event_tools.py the eight tools, split between the two specialists
schema/
  agent.sql        paste-able into the Supabase SQL editor
  events.sql
scripts/
  demo.py          the whole project in one command
  worker.py        a worker
  ask.py           queue a question, wait for the answer
  cancel.py        cancel a run from another terminal
  setup_supabase.py  create the schemas and seed
tests/             76 tests, about ten seconds, no key and no network
```
