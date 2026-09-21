"""Queue a question and wait for a worker to answer it.

    python -m scripts.ask --student 23CS101 "Is the Robotics Bootcamp still open?"

Prints the run id, which is what `python -m scripts.cancel` takes.
"""
import argparse
import time

from app.config import GEMINI_MODEL, open_stores
from app.memory import TERMINAL
from scripts._term import CYAN, DIM, GREEN, RED, RESET


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("text")
    p.add_argument("--student", default="23CS101")
    p.add_argument("--thread", help="continue an existing thread instead of starting one")
    p.add_argument("--timeout", type=float, default=120.0)
    a = p.parse_args()

    store, _ = open_stores()
    thread = a.thread or store.create_thread(a.student)
    run_id = store.enqueue(thread, a.text, GEMINI_MODEL)
    print(f"{DIM}thread {thread}{RESET}\n{CYAN}run {run_id}{RESET} queued; waiting for a worker...")

    deadline = time.time() + a.timeout
    while (run := store.get_run(run_id))["status"] not in TERMINAL:
        if time.time() > deadline:
            print(f"{RED}still {run['status']} after {a.timeout:.0f}s. Is a worker running?{RESET}")
            return
        time.sleep(0.5)

    if run["status"] == "succeeded":
        print(f"{GREEN}assistant>{RESET} {store.load_history(thread)[-1]['text']}")
    else:
        print(f"{RED}run ended: {run['status']} ({run['error_code']}){RESET}")


if __name__ == "__main__":
    main()
