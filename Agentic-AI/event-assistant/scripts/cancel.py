"""Cancel a run from a second terminal.

    python -m scripts.cancel <run-id>
    python -m scripts.cancel --list          # runs that can still be cancelled

This is the higher-grade item "cancel from a second terminal". It works because the run's state is
in the database rather than in the worker's memory: this process only sets a flag, and the worker
notices it between steps. A cancel never interrupts a tool half-way, so a seat is never taken
without a record of it.
"""
import argparse

from app.config import open_stores
from scripts._term import CYAN, DIM, GREEN, RED, RESET


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("run_id", nargs="?", help="the run id printed by scripts.ask")
    p.add_argument("--list", action="store_true", help="show queued and running runs")
    a = p.parse_args()

    store, _ = open_stores(migrate=False)

    if a.list or not a.run_id:
        rows = store.conn.execute(
            "SELECT id, status, attempts, lease_owner FROM run"
            " WHERE status IN ('queued', 'running') ORDER BY created_at").fetchall()
        if not rows:
            print(f"{DIM}nothing queued or running.{RESET}")
        for r in rows:
            print(f"{CYAN}{r['id']}{RESET} {r['status']}"
                  f"{DIM} attempt {r['attempts']}, worker {r['lease_owner'] or '-'}{RESET}")
        return

    status = store.request_cancel(a.run_id)
    if status is None:
        print(f"{RED}no run with id {a.run_id}{RESET}")
    elif status == "cancelled":
        print(f"{GREEN}cancelled{RESET} {DIM}(it was still queued, so it never started){RESET}")
    elif status == "running":
        print(f"{GREEN}cancel requested{RESET} {DIM}(the worker stops after its current step){RESET}")
    else:
        print(f"{DIM}run already finished: {status}. Nothing to cancel.{RESET}")


if __name__ == "__main__":
    main()
