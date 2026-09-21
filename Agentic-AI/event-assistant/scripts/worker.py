"""Run a worker against the real schemas.

    python -m scripts.worker --mock            # scripted models, no key
    python -m scripts.worker --mock --slow 2   # slow enough to Ctrl+C mid-run, for the crash drill
    python -m scripts.worker                   # Gemini

Start two of these and they will take different runs: the claim uses FOR UPDATE SKIP LOCKED.
"""
import argparse
import logging
import time

from app.config import make_providers, open_stores, using_supabase
from app.worker import Worker
from scripts._term import CYAN, DIM, GREEN, RED, RESET, print_step


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mock", action="store_true", help="scripted models instead of Gemini")
    p.add_argument("--slow", type=float, default=0.0, help="seconds to pause before each model turn")
    p.add_argument("--once", action="store_true", help="stop as soon as the queue is empty")
    p.add_argument("--lease", type=float, default=30.0, help="lease length in seconds")
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING)

    store, db = open_stores()
    worker = Worker(store, db, make_providers(a.mock, a.slow), lease_seconds=a.lease, on_step=print_step)
    where = "Supabase" if using_supabase() else "local Postgres"
    print(f"{CYAN}worker {worker.worker_id}{RESET} {DIM}on {where}; Ctrl+C to stop.{RESET}")
    try:
        while True:
            item = worker.run_once()
            if item is None:
                if a.once:
                    break
                time.sleep(1.0)
                continue
            colour = GREEN if item[1] == "succeeded" else RED
            print(f"{colour}run {item[0][:8]} {item[1]}{RESET}\n")
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
