"""The events office's tools, split between two specialist agents.

A tool description is a prompt. Each one below says when to use it, when not to, and what it
changes, because that text is the only thing the model has to go on when it decides.

The split is the least-privilege boundary: ProgrammeTools has no write tools at all, so the agent
holding it cannot take a seat no matter what it is asked to do.
"""
from datetime import datetime, timezone

from app.events_db import EventsDb
from app.idempotency import notification_dedupe_key
from app.tools.dispatch import dispatch


def _when(value) -> str:
    """Timestamps go to the model as ISO strings; a datetime is not JSON."""
    return value.isoformat() if isinstance(value, datetime) else str(value)


class Toolset:
    SIDE_EFFECTS: tuple[str, ...] = ()     # run through EventsDb.once with an idempotency key
    DELEGATES: tuple[str, ...] = ()        # hand work to another agent
    TOOL_NAMES: tuple[str, ...] = ()

    def functions(self) -> dict:
        return {n: getattr(self, n) for n in self.TOOL_NAMES}

    def call(self, name: str, args: dict) -> dict:
        return dispatch(self.functions(), name, args)


class ProgrammeTools(Toolset):
    """Read-only. The programme agent can look at the calendar, never change it."""

    TOOL_NAMES = ("search_events", "get_event")

    def __init__(self, db: EventsDb):
        self.db = db

    def search_events(self, text: str) -> dict:
        """Find events in the campus calendar by words from the title, category or venue.

        Use for "what workshops are on", "is there a hackathon", "anything in the Robotics Lab".
        Returns at most five matches. Read-only: changes nothing, and takes no seat. To register a
        student, that is the desk's job, not this tool. Do not use it to check whether a particular
        student is eligible; this tool knows nothing about students.

        Args:
            text: A few words from the title, category or venue, e.g. "robotics" or "workshop".

        Returns:
            {"events": [{"event_id", "title", "category", "venue", "starts_at", "seats_available"}]}.
            An empty list means no match; try fewer or different words. seats_available of 0 means
            the event is full and only the waitlist is left.
        """
        if not text.strip():
            return {"error": "empty_query", "hint": "Pass a few words from the title, category or venue."}
        return {"events": [{"event_id": e["id"], "title": e["title"], "category": e["category"],
                            "venue": e["venue"], "starts_at": _when(e["starts_at"]),
                            "seats_available": e["seats_available"]}
                           for e in self.db.search_events(text)]}

    def get_event(self, event_id: int) -> dict:
        """Get one event's details and how many seats are free right now.

        Use when you already have an event_id from search_events and need its current seat count
        before telling the student whether to register or to wait. Read-only: changes nothing, and
        a free seat reported here is not held for anyone.

        Args:
            event_id: Integer id returned by search_events.

        Returns:
            {"event_id", "title", "category", "venue", "starts_at", "seats_total", "seats_available"},
            or {"error": "unknown_event"} if no event has that id.
        """
        e = self.db.get_event(event_id)
        if e is None:
            return {"error": "unknown_event", "hint": "Use search_events to find the event_id first."}
        return {"event_id": e["id"], "title": e["title"], "category": e["category"], "venue": e["venue"],
                "starts_at": _when(e["starts_at"]), "seats_total": e["seats_total"],
                "seats_available": e["seats_available"]}


class DeskTools(Toolset):
    """The registration desk, bound to ONE student. The model cannot pick a different roll number:
    there is no parameter for it on any tool here."""

    TOOL_NAMES = ("get_student", "check_eligibility", "register_for_event", "join_waitlist",
                  "cancel_registration", "notify_student")
    SIDE_EFFECTS = ("register_for_event", "join_waitlist", "cancel_registration", "notify_student")

    def __init__(self, db: EventsDb, roll_no: str, clock=lambda: datetime.now(timezone.utc)):
        self.db, self.roll_no, self.clock = db, roll_no, clock

    def _student(self) -> dict:
        s = self.db.get_student(self.roll_no)
        if s is None:
            raise LookupError(f"student {self.roll_no} not found")
        return s

    # ------------------------------------------------------------------ read-only

    def get_student(self) -> dict:
        """Get the current student's record: name, department, attendance, registrations, waitlist.

        Use for "what am I signed up for", "what is my attendance", "where am I on the waitlist".
        Read-only: changes nothing. It does not decide whether the student may register; that is
        check_eligibility, which applies the office's policy.

        Returns:
            {"roll_no", "name", "dept", "attendance_pct", "max_active_registrations",
             "registrations": [{"event_id", "title"}], "waitlist": [{"event_id", "title", "position"}]}.
        """
        s = self._student()
        return {"roll_no": s["roll_no"], "name": s["name"], "dept": s["dept"],
                "attendance_pct": s["attendance_pct"],
                "max_active_registrations": s["max_active_registrations"],
                "registrations": [{"event_id": r["event_id"], "title": r["title"]}
                                  for r in self.db.active_registrations(s["id"])],
                "waitlist": [{"event_id": w["event_id"], "title": w["title"], "position": w["position"]}
                             for w in self.db.waitlist_entries(s["id"])]}

    def check_eligibility(self) -> dict:
        """Decide whether the current student may take another seat, using the office's policy.

        Use BEFORE register_for_event, and whenever the student asks "can I sign up". The decision
        comes from the policy table and the student's own limit: never decide it yourself, and never
        argue with the reasons it returns. Read-only: changes nothing, and an answer of True does not
        hold a seat, so an event can still fill up between this call and the registration.

        Returns:
            {"eligible": bool, "reasons": [str]}. Every reason is a rule the student currently breaks.
            An empty list of reasons means eligible.
        """
        s = self._student()
        reasons = []
        floor = self.db.policy("min_attendance_to_register")
        if s["attendance_pct"] < floor:
            reasons.append(f"attendance {s['attendance_pct']}% is below the {floor}% required")
        held = len(self.db.active_registrations(s["id"]))
        if held >= s["max_active_registrations"]:
            reasons.append(f"already holds {held} of {s['max_active_registrations']} allowed registrations")
        return {"eligible": not reasons, "reasons": reasons}

    # ------------------------------------------------------------------ side effects

    def register_for_event(self, event_id: int) -> dict:
        """Take one seat at an event for the current student. CHANGES DATA: a seat leaves the pool.

        Use only when the student has asked to attend this event and check_eligibility allowed it.
        Asking again for the same event is safe and returns the existing registration rather than
        taking a second seat. Do not use it for a full event: use join_waitlist instead. This tool
        re-checks the policy itself, so skipping check_eligibility does not get anyone in.

        Args:
            event_id: Integer id returned by search_events.

        Returns:
            {"event_id", "title", "status": "registered" | "already_registered"}, or an error:
            not_allowed (with reasons), unknown_event, or no_seats (the event filled up).
        """
        verdict = self.check_eligibility()
        if not verdict["eligible"]:
            return {"error": "not_allowed", "reasons": verdict["reasons"],
                    "hint": "Explain the reasons to the student. Do not retry."}
        event = self.db.get_event(event_id)
        if event is None:
            return {"error": "unknown_event", "hint": "Ask the programme agent for the right event_id."}
        status = self.db.register(self._student()["id"], event_id)
        if status == "no_seats":
            return {"error": "no_seats", "title": event["title"],
                    "hint": "The event is full. Offer join_waitlist; do not retry this tool."}
        return {"event_id": event_id, "title": event["title"], "status": status}

    def join_waitlist(self, event_id: int) -> dict:
        """Put the current student in the queue for a full event. CHANGES DATA: a place is taken.

        Use only after register_for_event has come back with no_seats, or when search_events showed
        seats_available of 0. Joining twice is safe and returns the place already held. This is not a
        registration: it takes no seat and guarantees nothing.

        Args:
            event_id: Integer id returned by search_events.

        Returns:
            {"event_id", "title", "position", "status": "waitlisted" | "already_waitlisted"}, or an
            error: unknown_event, has_seats (register instead), or waitlist_limit (with reasons).
        """
        event = self.db.get_event(event_id)
        if event is None:
            return {"error": "unknown_event", "hint": "Ask the programme agent for the right event_id."}
        if event["seats_available"] > 0:
            return {"error": "has_seats", "seats_available": event["seats_available"],
                    "hint": "Seats are free. Use register_for_event instead."}
        student = self._student()
        cap = self.db.policy("max_waitlist_per_student")
        existing = self.db.waitlist_entries(student["id"])
        if len(existing) >= cap and not any(w["event_id"] == event_id for w in existing):
            return {"error": "waitlist_limit",
                    "reasons": [f"already on {len(existing)} of {cap} allowed waitlists"],
                    "hint": "Tell the student to leave another waitlist first. Do not retry."}
        status, position = self.db.add_to_waitlist(student["id"], event_id)
        return {"event_id": event_id, "title": event["title"], "position": position, "status": status}

    def cancel_registration(self, event_id: int) -> dict:
        """Give back the current student's seat at an event. CHANGES DATA: a seat returns to the pool.

        Use when the student asks to withdraw, and only for an event they are registered for.
        Cancelling twice is safe: the second call reports not_registered and gives back no second
        seat. Do not use it to leave a waitlist, and never call it to "make room" for someone else.

        Args:
            event_id: Integer id of an event the student is registered for.

        Returns:
            {"event_id", "title", "status": "cancelled" | "not_registered"}, or unknown_event.
        """
        event = self.db.get_event(event_id)
        if event is None:
            return {"error": "unknown_event", "hint": "Ask the programme agent for the right event_id."}
        status = self.db.cancel_registration(self._student()["id"], event_id)
        return {"event_id": event_id, "title": event["title"], "status": status}

    def notify_student(self, message: str) -> dict:
        """Send the current student a short text message. CHANGES DATA: a message goes out.

        Use to confirm something that just happened, such as a registration or a waitlist place. The
        same message to the same student on the same day is sent only once. Never use it to answer a
        question or to continue the conversation; reply in the chat instead.

        Args:
            message: 1 to 160 characters.

        Returns:
            {"notification_id", "status": "queued", "duplicate": bool}. duplicate True means this
            exact message already went out today and nothing new was sent.
        """
        if not message.strip() or len(message) > 160:
            return {"error": "invalid_message", "hint": "message must be 1 to 160 characters."}
        key = notification_dedupe_key(self.roll_no, message, self.clock().date())
        notification_id, created = self.db.record_notification(self.roll_no, message, key)
        return {"notification_id": notification_id, "status": "queued", "duplicate": not created}
