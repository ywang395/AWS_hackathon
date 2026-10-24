"""
tools/downtime_log.py -- the shared downtime log the nurse agent writes to.

Backs the `DowntimeLog` table (CLAUDE.md section 5: PK patient_id, SK ts;
fields nurse_id, type, detail, double_checked_by) and implements the
double-dose safety check described in the nurse agent's system prompt
(`agents/nurse_agent/prompt.md`, from CLAUDE.md section 12):

    "Before confirming a med_given log, call check_recent_doses. If
    already given, say STOP and who/when."

Two tools:
  - `log_event()`          -- append an entry (med given / vital / note)
  - `check_recent_doses()` -- look back over the log for a med already
                               given to this patient within an unsafe window

Like `tools/patients.py`, storage is injectable (`store=`) so Phase 1 can
run entirely in memory for local dev/tests, and Phase 2 can swap in a
DynamoDB-backed `LogStore` (via `shared/db.py`) without changing any
caller -- including `tools/patients.py` ownership checks, which should run
BEFORE `patient_id` ever reaches these functions.

Callers pass `store=` explicitly. `default_log_store()` returns the shared
store for the running process: the `DowntimeLog` table when
DATA_BACKEND=dynamodb, otherwise one process-wide InMemoryLogStore (the
single-process dev/demo server, CLAUDE.md section 3). The log must be shared
across nurses and requests, because the double-dose check depends on seeing
every dose given to a patient. Tests pass their own store, so state never
leaks between them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Optional, Protocol

from backend.shared import db

EventType = Literal["med_given", "vital", "note"]

# Clinically, a "double dose" means the same medication was logged again
# before its next scheduled dose would plausibly be due. 4 hours is a
# conservative default lookback that comfortably covers the shortest
# dosing interval in data/patients.json; pass `window_minutes=` to a
# specific call if a particular medication needs a tighter/looser window.
DEFAULT_DOUBLE_DOSE_WINDOW_MINUTES = 240


@dataclass
class LogEntry:
    patient_id: str
    ts: str  # ISO 8601 UTC -- also the DynamoDB sort key
    nurse_id: str
    type: EventType
    detail: str
    double_checked_by: Optional[str] = None


class LogStore(Protocol):
    def append(self, entry: LogEntry) -> None: ...

    def entries_for(self, patient_id: str) -> list[LogEntry]: ...


class InMemoryLogStore:
    """
    Default `LogStore` implementation. Good enough for Phase 1 / a single
    test run's worth of state; Phase 2 should swap in a DynamoDB-backed
    store (`shared/db.py`) since separate Lambda invocations do not share
    memory.
    """

    def __init__(self) -> None:
        self._entries: dict[str, list[LogEntry]] = {}

    def append(self, entry: LogEntry) -> None:
        self._entries.setdefault(entry.patient_id, []).append(entry)

    def entries_for(self, patient_id: str) -> list[LogEntry]:
        return list(self._entries.get(patient_id, []))


class DynamoLogStore:
    """`DowntimeLog` table (CLAUDE.md section 5): PK patient_id, SK ts."""

    def append(self, entry: LogEntry) -> None:
        db.put_item("downtime_log", {k: v for k, v in asdict(entry).items() if v is not None})

    def entries_for(self, patient_id: str) -> list[LogEntry]:
        names = [f.name for f in fields(LogEntry)]
        return [
            LogEntry(**{name: row.get(name) for name in names})
            for row in db.query("downtime_log", "patient_id", patient_id)
        ]


_process_log_store = InMemoryLogStore()


def default_log_store() -> LogStore:
    """The shared log for this process (see module docstring)."""
    return DynamoLogStore() if db.use_dynamodb() else _process_log_store


def _now_iso() -> str:
    # Unique per write: a second entry with the same (patient_id, ts) key
    # would silently overwrite the first in DynamoDB.
    return db.unique_timestamp()


def _parse_iso(ts: str) -> datetime:
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def log_event(
    nurse_id: str,
    patient_id: str,
    event_type: EventType,
    detail: str,
    *,
    store: LogStore,
    double_checked_by: Optional[str] = None,
    now: Optional[str] = None,
) -> LogEntry:
    """
    Append one entry to the downtime log. This is a pure append -- it does
    NOT itself block a duplicate `med_given`. The system prompt requires
    the agent to call `check_recent_doses` FIRST and decide whether to
    proceed; enforcing the block here too, as defense in depth, is a
    reasonable Phase-2 hardening step once the UI/agent flow is finalized.
    """
    entry = LogEntry(
        patient_id=patient_id,
        ts=now or _now_iso(),
        nurse_id=nurse_id,
        type=event_type,
        detail=detail,
        double_checked_by=double_checked_by,
    )
    store.append(entry)
    return entry


def check_recent_doses(
    patient_id: str,
    med: str,
    *,
    store: LogStore,
    window_minutes: int = DEFAULT_DOUBLE_DOSE_WINDOW_MINUTES,
    now: Optional[str] = None,
) -> list[LogEntry]:
    """
    Return every `med_given` entry for `med` on `patient_id` within the
    last `window_minutes`, most recent first. An empty list means it is
    safe to log the new dose; a non-empty list means STOP -- the agent
    should report who gave it and when instead of logging a new entry.

    Matching on `med` is a case-insensitive substring match against
    `detail` (entries are written as free text, e.g. "Insulin 6 units
    SubQ"), so the agent does not need to pass a normalized med code.
    """
    reference = _parse_iso(now) if now else datetime.now(timezone.utc)
    cutoff = reference - timedelta(minutes=window_minutes)

    matches = [
        e
        for e in store.entries_for(patient_id)
        if e.type == "med_given" and med.lower() in e.detail.lower() and _parse_iso(e.ts) >= cutoff
    ]
    # Sort on parsed time: second- and microsecond-precision strings do not sort correctly as text.
    matches.sort(key=lambda e: _parse_iso(e.ts), reverse=True)
    return matches


def bind_downtime_log_tools(
    nurse_id: str,
    patient_id: str,
    store: Optional[LogStore] = None,
) -> dict[str, Callable]:
    """
    Build the `tool_impls` dict for `run_agent()` scoped to one nurse/
    patient pair, in the same spirit as
    `tools.patients.bind_patient_tools` -- closes over `nurse_id` so the
    model cannot log an entry under someone else's name, and over
    `patient_id` so it cannot write to a chart it does not own. Callers
    should obtain `patient_id` via `tools.patients.get_patient`'s
    ownership check FIRST, then pass the result in here.
    """
    active_store = store if store is not None else InMemoryLogStore()

    def _log_event(event_type: EventType, detail: str, double_checked_by: Optional[str] = None) -> dict:
        entry = log_event(
            nurse_id,
            patient_id,
            event_type,
            detail,
            store=active_store,
            double_checked_by=double_checked_by,
        )
        return entry.__dict__

    def _check_recent_doses(med: str, window_minutes: int = DEFAULT_DOUBLE_DOSE_WINDOW_MINUTES) -> list[dict]:
        matches = check_recent_doses(patient_id, med, store=active_store, window_minutes=window_minutes)
        return [m.__dict__ for m in matches]

    return {
        "log_event": _log_event,
        "check_recent_doses": _check_recent_doses,
    }