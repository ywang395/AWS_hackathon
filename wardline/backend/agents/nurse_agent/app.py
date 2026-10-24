"""
backend/agents/nurse_agent/app.py -- the Nurse agent, served as POST /chat.

CLAUDE.md section 7 contract (do not change without telling the team):

    POST /chat {token, pin, message}
      -> {reply, snapshot_ts, brief?: [..], patients: [..], deliveries: [..]}
      (first call with message="" returns the downtime brief)

Request flow (handle_chat):
  1. shared.auth.authenticate(token, pin) gives the verified nurse_id. That
     is the only way nurse_id enters the system (CLAUDE.md section 2).
  2. A valid link + PIN acknowledges the outage alert, so the alert agent
     stops resending (agents/alert_agent/app.py::record_ack).
  3. Only this nurse's patients are loaded (tools/patients.py), before
     anything reaches the model.
  4. message == ""  -> downtime brief built in code from the records. No
                       model call: it works while Bedrock is unreachable and
                       cannot invent anything.
     otherwise      -> shared.agent_loop.AgentLoop with the section 8 nurse
                       tools, each closed over the verified nurse_id.
  5. Every reply carries the snapshot time (CLAUDE.md section 2). Code
     appends it when the model leaves it out.

Hosting: CLAUDE.md section 3 ruled out Lambda in the workshop account; the
backend runs as one server (proposed backend/server.py). handle_chat() is
host-agnostic -- a FastAPI/Flask route calls it with the parsed JSON body
and maps ChatError to an HTTP status. http_handler() wraps it for a plain
HTTP request/response (JSON body in, {status, headers, body} out); it keeps
the proxy-event shape so it also works unchanged behind an HTTP gateway if
one is ever added.

Every collaborator is injectable through ChatDeps, so tests, the local demo
and a future server can swap storage, Bedrock and event publishing without
editing this module. Defaults follow DATA_BACKEND (shared/db.py).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol

from backend.agents.nurse_agent.tool_specs import NURSE_TOOL_SPECS
from backend.shared import auth, db
from backend.shared.agent_loop import AgentFeedLogger, AgentLoop, AgentLoopError
from backend.shared.events import put_event
from backend.tools import deliveries as delivery_tools
from backend.tools import downtime_log
from backend.tools.patients import get_my_patients, get_patient

AGENT_NAME = "nurse_agent"  # also the EventBridge source of MedRequested (CLAUDE.md section 6)
PROMPT_PATH = Path(__file__).with_name("prompt.md")
NURSES_PATH = Path(__file__).resolve().parents[3] / "data" / "nurses.json"
HISTORY_TURNS = 6  # recent turns replayed to the model; AgentLoop takes one user message
EVENT_TYPES = frozenset({"med_given", "vital", "note"})  # DowntimeLog.type values (section 5)
CORS_HEADERS = {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"}


class ChatError(Exception):
    """A request /chat refuses. `status` is the HTTP code the host should return."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _now_iso() -> str:
    # Unique per write: the nurse turn and the reply are stored back to back,
    # and a shared ChatHistory sort key would let the reply overwrite the turn.
    return db.unique_timestamp()


# ---------------------------------------------------------------------------
# Chat history (`ChatHistory` table: PK token, SK ts)
# ---------------------------------------------------------------------------

class ChatHistoryStore(Protocol):
    def append(self, token: str, role: str, content: str) -> None: ...

    def recent(self, token: str, limit: int) -> list[dict]: ...


class InMemoryChatHistory:
    def __init__(self) -> None:
        self._turns: dict[str, list[dict]] = {}

    def append(self, token: str, role: str, content: str) -> None:
        self._turns.setdefault(token, []).append({"ts": _now_iso(), "role": role, "content": content})

    def recent(self, token: str, limit: int) -> list[dict]:
        return list(self._turns.get(token, [])[-limit:])


class DynamoChatHistory:
    def append(self, token: str, role: str, content: str) -> None:
        db.put_item("chat_history", {"token": token, "ts": _now_iso(), "role": role, "content": content})

    def recent(self, token: str, limit: int) -> list[dict]:
        rows = db.query("chat_history", "token", token, newest_first=True, limit=limit)
        return list(reversed(rows))  # oldest first, in conversation order


_process_history = InMemoryChatHistory()


def default_history() -> ChatHistoryStore:
    return DynamoChatHistory() if db.use_dynamodb() else _process_history


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def _record_ack_default(nurse_id: str) -> None:
    # Imported on use: the alert agent is only needed once a chat authenticates.
    from backend.agents.alert_agent import app as alert_app

    alert_app.record_ack(nurse_id, store=alert_app.get_ack_store())


@dataclass
class ChatDeps:
    """Everything handle_chat talks to. A None field falls back to the DATA_BACKEND default."""

    token_store: Optional[auth.TokenStore] = None        # -> auth.default_token_store()
    nurse_lookup: Optional[auth.NurseLookup] = None      # -> auth.default_nurse_lookup()
    log_store: Optional[downtime_log.LogStore] = None    # -> downtime_log.default_log_store()
    history: Optional[ChatHistoryStore] = None           # -> default_history()
    # Same signature as shared.events.put_event(detail_type, source, detail). A
    # single-process server can wrap it to also run the routing agent in-process.
    publish_event: Callable[[str, str, Mapping[str, Any]], Any] = put_event
    record_ack: Optional[Callable[[str], None]] = _record_ack_default  # None disables acking
    load_deliveries: Optional[Callable[[], list[dict]]] = None  # -> Deliveries table or tools.deliveries
    patients: Optional[list[dict]] = None  # roster override (tests); None -> tools.patients loaders
    nurses: Optional[list[dict]] = None    # staff override for name lookups (tests)
    bedrock_client: Any = None             # None -> real bedrock-runtime client (agent_loop)
    model_id: Optional[str] = None         # None -> MODEL_ID env var
    feed_logger: Optional[AgentFeedLogger] = None  # None -> JSON log lines


def _all_deliveries(deps: ChatDeps) -> list[dict]:
    if deps.load_deliveries is not None:
        return deps.load_deliveries()
    if db.use_dynamodb():
        # Populated once the routing agent's create_delivery writes to this
        # table (its Phase 2 swap). Until then deliveries live in its memory.
        return db.scan("deliveries")
    return delivery_tools.list_deliveries()


def _nurse_name(deps: ChatDeps, nurse_id: str) -> str:
    if deps.nurses is not None:
        staff = deps.nurses
    elif db.use_dynamodb():
        item = db.get_item("nurses", nurse_id=nurse_id)
        return item.get("name", nurse_id) if item else nurse_id
    else:
        staff = json.loads(NURSES_PATH.read_text(encoding="utf-8"))
    return next((n.get("name", nurse_id) for n in staff if n.get("nurse_id") == nurse_id), nurse_id)


def _snapshot_ts(patients: list[dict]) -> Optional[str]:
    stamps = [p["snapshot_ts"] for p in patients if p.get("snapshot_ts")]
    return max(stamps) if stamps else None  # ISO 8601 UTC strings sort chronologically


def _display_ts(snapshot_ts: Optional[str]) -> str:
    """'02:05 UTC'. The frontend shows snapshot_ts in local time; replies stay unambiguous."""
    if not snapshot_ts:
        return "an unknown time"
    return datetime.fromisoformat(snapshot_ts.replace("Z", "+00:00")).strftime("%H:%M UTC")


def build_brief(patients: list[dict]) -> list[dict]:
    """One card per patient in room order. Facts copied from the record, nothing inferred."""
    brief = []
    for patient in sorted(patients, key=lambda p: str(p.get("room"))):
        meds = sorted(patient.get("meds", []), key=lambda m: m.get("due_at") or "")
        brief.append({
            "room": patient.get("room"),
            "patient_id": patient.get("patient_id"),
            "name": patient.get("name"),
            "allergies": list(patient.get("allergies", [])),
            "next_due": meds[0] if meds else None,
            "critical_meds": [m.get("med") for m in meds if m.get("critical")],
            "notes": patient.get("notes", ""),
        })
    return brief

# ---------------------------------------------------------------------------
# Tools (CLAUDE.md section 8), each closed over the verified nurse_id
# ---------------------------------------------------------------------------

def build_tool_impls(nurse_id: str, deps: ChatDeps) -> dict[str, Callable[..., Any]]:
    """tool_impls for AgentLoop.run. Keys match tool_specs.NURSE_TOOL_SPECS.

    Every room-based tool goes through tools.patients.get_patient, which
    raises PatientAccessDenied for another nurse's room. AgentLoop hands that
    back to the model as an error carrying only the exception type, so the
    refused chart never reaches it.
    """
    log_store = deps.log_store or downtime_log.default_log_store()

    def owned(room: str) -> dict:
        return get_patient(nurse_id, str(room).strip(), patients=deps.patients)

    def dose_view(entry: downtime_log.LogEntry) -> dict:
        return {
            "given_by": _nurse_name(deps, entry.nurse_id),
            "nurse_id": entry.nurse_id,
            "ts": entry.ts,
            "detail": entry.detail,
            "double_checked_by": entry.double_checked_by,
        }

    def get_my_patients_tool() -> list[dict]:
        return get_my_patients(nurse_id, patients=deps.patients)

    def get_patient_tool(room: str) -> dict:
        return owned(room)

    def check_recent_doses_tool(room: str, med: str) -> list[dict]:
        patient = owned(room)
        return [dose_view(e) for e in downtime_log.check_recent_doses(patient["patient_id"], med, store=log_store)]

    def log_event_tool(room: str, event_type: str, detail: str, double_checked_by: Optional[str] = None) -> dict:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"event_type must be one of {sorted(EVENT_TYPES)}.")
        patient = owned(room)
        if event_type == "med_given":
            med = _ordered_med_in(patient, detail)
            if med is None:
                return {"status": "not_in_record",
                        "message": f"No ordered medication for room {patient['room']} matches '{detail}'."}
            # Enforced in code, not only in the prompt: a repeat dose is never logged.
            # The log is shared, so a dose another nurse logged also blocks this one.
            prior = downtime_log.check_recent_doses(patient["patient_id"], med, store=log_store)
            if prior:
                last = dose_view(prior[0])
                return {
                    "status": "stop", "med": med, "room": patient["room"], **last,
                    "message": f"STOP: {med} was already given to room {patient['room']} "
                               f"by {last['given_by']} at {last['ts']}.",
                }
        entry = downtime_log.log_event(nurse_id, patient["patient_id"], event_type, detail,
                                       store=log_store, double_checked_by=double_checked_by)
        return {"status": "logged", "room": patient["room"], **asdict(entry)}

    def request_med_tool(room: str, med: str) -> dict:
        patient = owned(room)
        order = next((m for m in patient.get("meds", [])
                      if m.get("med", "").casefold() == med.strip().casefold()), None)
        if order is None:
            return {"status": "not_in_record", "message": f"{med} is not ordered for room {patient['room']}."}
        # due_at comes from the record, never from the model, and the ID is the one
        # create_delivery builds (tools/deliveries.py::build_request_id), so the
        # routing agent's delivery dedupes against this request.
        detail = {
            "request_id": delivery_tools.build_request_id(patient["room"], order["med"], order["due_at"]),
            "room": str(patient["room"]),
            "med": order["med"],
            "requested_by": nurse_id,
        }
        deps.publish_event("MedRequested", AGENT_NAME, detail)  # CLAUDE.md section 6 -> routing agent
        return {"status": "requested", "due_at": order["due_at"], **detail}

    def get_delivery_status_tool(room: str) -> list[dict]:
        patient = owned(room)
        return [d for d in _all_deliveries(deps) if str(d.get("to_room")) == str(patient["room"])]

    return {
        "get_my_patients": get_my_patients_tool,
        "get_patient": get_patient_tool,
        "check_recent_doses": check_recent_doses_tool,
        "log_event": log_event_tool,
        "request_med": request_med_tool,
        "get_delivery_status": get_delivery_status_tool,
    }


def _ordered_med_in(patient: dict, detail: str) -> Optional[str]:
    """Name of the ordered medication mentioned in a free-text log detail, if any."""
    text = detail.casefold()
    return next((m["med"] for m in patient.get("meds", []) if m.get("med") and m["med"].casefold() in text), None)


# ---------------------------------------------------------------------------
# POST /chat
# ---------------------------------------------------------------------------

def handle_chat(request: Mapping[str, Any], deps: Optional[ChatDeps] = None) -> dict[str, Any]:
    """Serve one POST /chat request. Returns the section 7 response or raises ChatError."""
    deps = deps or ChatDeps()
    token, pin, message = _parse_request(request)

    try:
        nurse_id = auth.authenticate(token, pin, store=deps.token_store, nurse_lookup=deps.nurse_lookup)
    except auth.AuthError as exc:
        # One answer for every failure: the client must not learn whether the link or the PIN was wrong.
        raise ChatError(401, "Invalid link or PIN.") from exc

    _acknowledge(deps, nurse_id)

    patients = get_my_patients(nurse_id, patients=deps.patients)
    snapshot_ts = _snapshot_ts(patients)
    shown_ts = _display_ts(snapshot_ts)
    rooms = {str(p.get("room")) for p in patients}
    response: dict[str, Any] = {
        "reply": "",
        "snapshot_ts": snapshot_ts,
        "patients": patients,
        "deliveries": [d for d in _all_deliveries(deps) if str(d.get("to_room")) in rooms],
    }

    if not message.strip():
        response["brief"] = build_brief(patients)
        response["reply"] = f"Downtime brief: {len(patients)} patient(s) assigned to you. Records as of {shown_ts}."
        return response

    history = deps.history or default_history()
    reply = _ask_agent(nurse_id, token, message, shown_ts, deps, history)
    history.append(token, "nurse", message)
    history.append(token, "assistant", reply)
    response["reply"] = reply
    return response


def _parse_request(request: Any) -> tuple[str, str, str]:
    if not isinstance(request, Mapping):
        raise ChatError(400, "Request body must be a JSON object.")
    token, pin, message = request.get("token"), request.get("pin"), request.get("message", "")
    if not isinstance(token, str) or not token.strip() or not isinstance(pin, str) or not pin.strip():
        raise ChatError(400, "token and pin are required strings.")
    if not isinstance(message, str):
        raise ChatError(400, "message must be a string.")
    return token.strip(), pin.strip(), message


def _acknowledge(deps: ChatDeps, nurse_id: str) -> None:
    if deps.record_ack is None:
        return
    try:
        deps.record_ack(nurse_id)
    except Exception as exc:  # noqa: BLE001
        # A failed ack write must not lock a nurse out of their patients during
        # an outage. The alert agent resends and escalates instead.
        print(json.dumps({"event": "ack_failed", "agent": AGENT_NAME, "nurse_id": nurse_id, "error": str(exc)}))


def _ask_agent(nurse_id: str, token: str, message: str, shown_ts: str,
               deps: ChatDeps, history: ChatHistoryStore) -> str:
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8").replace("{snapshot_ts}", shown_ts)
    loop = AgentLoop(model_id=deps.model_id, bedrock_client=deps.bedrock_client,
                     feed_logger=deps.feed_logger, agent_name=AGENT_NAME)
    try:
        reply = loop.run(system_prompt, NURSE_TOOL_SPECS,
                         _with_history(history.recent(token, HISTORY_TURNS), message),
                         build_tool_impls(nurse_id, deps))
    except AgentLoopError as exc:
        # Bedrock or the loop failed: say so instead of returning a 500.
        # The patient list in the same response is still valid.
        print(json.dumps({"event": "agent_failed", "agent": AGENT_NAME, "error_type": type(exc).__name__}))
        return (f"The assistant is unavailable right now. The patient list below is from the records "
                f"as of {shown_ts}; follow the paper downtime procedure.")
    # Section 2: every answer shows the snapshot time, even if the model left it out.
    return reply if shown_ts in reply else f"{reply.rstrip()}\n(Records as of {shown_ts})"


def _with_history(turns: list[dict], message: str) -> str:
    if not turns:
        return message
    lines = ["Earlier in this conversation:"]
    lines += [f"{'Nurse' if t['role'] == 'nurse' else 'Wardline'}: {t['content']}" for t in turns]
    return "\n".join(lines) + f"\n\nNurse: {message}"


def http_handler(event: Mapping[str, Any], context: Any = None, *, deps: Optional[ChatDeps] = None) -> dict[str, Any]:
    """HTTP request adapter: JSON body in, {statusCode, headers, body} out.

    Shape matches an HTTP-gateway proxy event, so a FastAPI/Flask route can
    forward the raw request here and return the result directly. `context`
    is accepted and ignored for gateway/Lambda calling-convention parity.
    """
    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        status, payload = 200, handle_chat(json.loads(raw) if isinstance(raw, str) else raw, deps)
    except json.JSONDecodeError:
        status, payload = 400, {"error": "Request body must be valid JSON."}
    except ChatError as exc:
        status, payload = exc.status, {"error": exc.message}
    return {"statusCode": status, "headers": CORS_HEADERS, "body": json.dumps(payload, default=str)}