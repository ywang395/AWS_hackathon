# AWS_hackathon
Testing Kiro IDE

## Wardline -- Phase 1 status (`wardline/`)

See `wardline/CLAUDE.md` for the full project spec. This section tracks
what has been implemented so far and any workarounds taken along the way.

### Implemented (Alisher's Phase 1 checklist, CLAUDE.md section 9)

- `data/nurses.json`, `data/patients.json`, `data/inventory.json` --
  synthetic seed data: 3 nurses + 1 charge (Dana) + 2 runners (Marcus,
  Priya), 12 patients, 3 inventory locations. Demo cases are baked in:
  insulin missing from the Unit 4 Cabinet but in stock at the Unit 6
  Cabinet and Central Pharmacy (room 304, Logan Pierce), and heparin with
  zero stock everywhere (room 602, David Kowalski).
- `scripts/seed.py` -- loads `data/*.json` into DynamoDB via `boto3`
  (lazy import; only needed once `template.yaml` is deployed). Supports
  `--dry-run` and `--only <fixture>`.
- `backend/shared/auth.py` -- PBKDF2 PIN hashing + secure-link token
  creation/verification, with injectable storage so it runs with no AWS
  dependency in Phase 1.
- `backend/tools/patients.py` -- `get_my_patients` / `get_patient` with
  server-side ownership enforcement (never trusts a model-supplied
  `nurse_id`), plus `bind_patient_tools` for wiring into the Bedrock tool
  loop later.
- `backend/tools/downtime_log.py` -- `log_event` / `check_recent_doses`
  double-dose detection.
- `backend/agents/nurse_agent/prompt.md` -- system prompt (CLAUDE.md
  section 12) plus a tool reference list.
- `backend/shared/sms.py` -- `send_sms` / `send_bulk_sms` over Amazon SNS,
  honoring `SMS_DRY_RUN=1`; boto3 is only imported on the live-send path.
- `backend/agents/alert_agent/app.py` -- message builder, ack tracking
  (`Alerts`-table shape), resend logic, and `escalate()`, driven by
  `run_ack_sweep()` and exposed as Lambda `handler()`.
- Unit tests for all of the above live next to their modules as
  `test_*.py` (46 tests, run with `pytest` from the `wardline/` folder).
  A `conftest.py` at the `wardline/` root puts the project on `sys.path`
  so `backend.*` imports resolve without needing `__init__.py` files.

### Demo PIN (synthetic, not sensitive)

Every seeded nurse uses PIN `1234` for the downtime chat login. This is
throwaway synthetic data for the hackathon demo, not a real credential.

### Workarounds

- **`tools/escalation.py` not yet built.** That file is owned by the
  routing-agent workstream (CLAUDE.md section 9) and is still an empty
  placeholder. `agents/alert_agent/app.py::escalate()` tries to import
  `tools.escalation.escalate` first; if that import fails, it falls back
  to appending escalation records to a local list (or an in-memory
  fallback list) so the alert agent is fully functional and testable on
  its own right now. No code changes will be needed in `alert_agent` once
  the real `tools/escalation.py` lands -- the import just starts
  succeeding.
- **`shared/agent_loop.py` (the Bedrock Converse tool-use loop) is still a
  stub.** The alert and nurse agents' tool functions are written as plain,
  directly-callable Python (so they're independently testable today) but
  are also exposed as `tool_impls`-shaped dicts (`alert_agent.get_tool_impls()`,
  `tools.patients.bind_patient_tools()`, `tools.downtime_log.bind_downtime_log_tools()`)
  so wiring them into `run_agent()` later is a drop-in change, not a
  rewrite.
- **Local dev/test environment runs PowerShell on Windows, not a
  POSIX shell.** A couple of adjustments were needed purely for this dev
  machine, unrelated to the application code itself:
  - This workspace's terminal tool echoes commands back character-by-character
    while they execute, which can look like corrupted output even though
    the command ran correctly. Workaround used throughout development:
    redirect command output to a file (`> out.txt 2>&1`) and read the file
    back separately, rather than trusting the inline terminal transcript.
  - File writes used `[System.IO.File]::WriteAllText(path, content, (New-Object System.Text.UTF8Encoding $false))`
    instead of `Set-Content -Encoding utf8`, because Windows PowerShell's
    `utf8` encoding adds a byte-order-mark that breaks `json.load()` on
    the seed data files (Python's default `utf-8` decoder does not strip
    a BOM; `utf-8-sig` does, but there's no reason to require that of
    every reader). The UTF8Encoding override writes clean, BOM-less UTF-8
    that both Python's `json` module and `open(..., encoding="utf-8")`
    read without any special-casing.