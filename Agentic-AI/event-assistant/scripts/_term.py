import json
import os
import sys

DIM, CYAN, YELLOW, RED, GREEN, MAGENTA, RESET = "\033[2m", "\033[36m", "\033[33m", "\033[31m", "\033[32m", "\033[35m", "\033[0m"
if os.name == "nt":
    os.system("")            # ask the console host to interpret the ANSI colours above


def _use_utf8() -> None:
    """The step arrows are U+2190 and U+2192, which a Windows console left on cp1252 cannot encode.
    Without this the demo dies on the first delegation on a stock Windows Python."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):     # not a real stream, or already fine
            pass


_use_utf8()


def short(obj, limit=130) -> str:
    text = json.dumps(obj, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def print_step(step: dict) -> None:
    """Supervisor steps at the margin, specialist steps indented under the delegation that ran them."""
    agent = step.get("agent", "supervisor")
    if step["kind"] == "model":
        return
    if step["kind"] == "delegate":
        print(f"  {YELLOW}supervisor \u2192 {step['tool']}{RESET}{DIM}({short(step['args'], 100)}){RESET}")
    elif agent == "supervisor":
        answer = step["result"].get("answer") or step["result"].get("error")
        colour = DIM if step["ok"] else RED
        print(f"  {colour}           \u2190 {short(answer, 110)}{RESET}")
    else:
        colour = MAGENTA if step["ok"] else RED
        print(f"      {colour}{agent} \u2192 {step['tool']}{RESET}{DIM}({short(step['args'], 80)}){RESET}")
        note = f"  {GREEN}[replayed: stored result, nothing done again]{RESET}" if step.get("replayed") else ""
        print(f"      {DIM}{' ' * len(agent)} \u2190 {short(step['result'], 100)}{RESET}{note}")


def redact(url: str) -> str:
    """A Postgres URL carries the password. Never print one without running it through here."""
    if "@" not in url:
        return url
    head, tail = url.rsplit("@", 1)
    scheme, _, creds = head.partition("://")
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{tail}"
