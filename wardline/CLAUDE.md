# CLAUDE.md — Wardline

> "Most continuity tools protect data. We protect the dose."

SpartHill Hacks 2026 · Track: AI-Driven Automation & Critical Service Continuity (AWS)

## 1. What we're building

When ransomware forces a hospital to shut down its records and pharmacy systems, Wardline keeps medication care running:

1. **Alert agent** — emails every on-shift nurse a secure link, requires acknowledgment, resends, escalates.
2. **Nurse agent** — per-nurse AI assistant scoped to ONLY that nurse's patients: downtime brief, Q&A, dose logging, double-dose check, med requests.
3. **Routing agent** — finds where stock exists, prioritizes doses due, dispatches runners by email, escalates when no stock exists.
4. **Recovery agent** (stretch) — reconciliation report when systems return.

Persona: **Dana**, night-shift charge nurse, 6 nurses, 32 patients, systems dark at 2 AM. Runner: **Marcus**.

Wardline supports the hospital's downtime procedures (paper MAR stays the official record). It coordinates, checks, and routes.

## 2. Non-negotiable rules (Claude: never violate these)

- **Synthetic data only.** No real names, MRNs, or PHI anywhere.
- **Patient scoping is enforced in code, never in prompts.** `nurse_id` comes from the verified token server-side. No tool accepts `nurse_id` as a model-supplied argument. Data is filtered before the model sees it.
- **The AI never changes doses, never substitutes drugs, never diagnoses.** Those go to `escalate()`.
- **Every AI answer shows the snapshot timestamp** ("Records as of 2:05 AM").
- **If it's not in the record, say "Not in the record"** — never guess.
- No secrets in code. Config via env vars.
- Idempotency: deliveries keyed by `request_id = {room}-{med}-{due_at}` so duplicates never create two deliveries.

## 3. Stack

| Layer | Choice |
|---|---|
| Language (backend) | Python 3.12, boto3 |
| AI | Amazon Bedrock, **Converse API with tool use**, Claude Haiku. `MODEL_ID` env var = **`us.anthropic.claude-haiku-4-5-20251001-v1:0`** (us inference profile; bare model ID is not on-demand invocable) |
| Compute | AWS Lambda (one function per agent + API handlers) |
| Events | EventBridge custom bus `wardline` + EventBridge Scheduler (5-min routing scan, ack checks) |
| Data | DynamoDB (on-demand) |
| Email | Amazon SES v2 `send_email` (sandbox: **verify every demo email address**, both sender and recipients, until production access granted). Replaces SMS — the workshop account has no SMS origination and a non-raisable $1 SMS cap. |
| API | API Gateway HTTP API, CORS enabled |
| Frontend | React + Vite (static build), hosted on S3 static website |
| IaC | AWS SAM (`template.yaml`) |
| Region | `us-east-1` (everything in ONE region with Bedrock model access enabled) |

## 4. Repo structure

```
wardline/
├── CLAUDE.md
├── template.yaml                 # SAM: all Lambdas, tables, bus, rules, API
├── backend/
│   ├── shared/
│   │   ├── agent_loop.py         # Bedrock Converse tool-use loop (shared by all agents)
│   │   ├── db.py                 # DynamoDB helpers
│   │   ├── events.py             # put_event() helper
│   │   ├── notify.py             # SES send_email helper (+ DRY_RUN mode)  [formerly sms.py]
│   │   └── auth.py               # token + PIN verification
│   ├── agents/
│   │   ├── alert_agent/app.py
│   │   ├── nurse_agent/app.py    # = chat_handler (API)
│   │   ├── routing_agent/app.py
│   │   └── recovery_agent/app.py
│   ├── api/
│   │   ├── dashboard/app.py      # GET /dashboard
│   │   ├── control/app.py        # POST /control/outage, /control/restore
│   │   └── deliveries/app.py     # POST /deliveries/{id}/done
│   └── tools/                    # tool functions agents call (pure Python)
│       ├── patients.py
│       ├── stock.py
│       ├── deliveries.py
│       ├── downtime_log.py
│       └── escalation.py
├── frontend/
│   └── src/
│       ├── pages/NurseView.jsx
│       ├── pages/Dashboard.jsx
│       ├── pages/Control.jsx
│       └── api.js                # all fetch calls; MOCK mode via VITE_MOCK=1
├── data/
│   ├── nurses.json
│   ├── patients.json
│   └── inventory.json
└── scripts/
    ├── seed.py                   # loads data/ into DynamoDB
    └── reset.py                  # clears logs/deliveries/alerts for demo rerun
```

## 5. Data model (DynamoDB)

| Table | Keys | Fields |
|---|---|---|
| `Nurses` | PK `nurse_id` | name, email, pin_hash, unit, role (`nurse`/`charge`/`runner`), on_shift, location |
| `Patients` | PK `patient_id`; GSI `assigned_nurse_id` | room, name (synthetic), dob (for 2-identifier check), allergies[], meds[{med, dose, route, due_at, critical}], vitals, notes, snapshot_ts |
| `Inventory` | PK `location`, SK `med` | qty, distance_rank |
| `Deliveries` | PK `request_id` | med, from_location, to_room, due_at, priority, runner_id, status (`assigned`/`picked_up`/`delivered`), created_at |
| `DowntimeLog` | PK `patient_id`, SK `ts` | nurse_id, type (`med_given`/`vital`/`note`), detail, double_checked_by |
| `Tokens` | PK `token` | nurse_id, expires_at (TTL attr) |
| `Alerts` | PK `nurse_id`, SK `sent_at` | attempts, acked, acked_at |
| `Escalations` | PK `escalation_id` | source_agent, reason, room, created_at, resolved |
| `AgentFeed` | PK `day`, SK `ts` | agent, action, detail — **every agent decision is written here for the live dashboard** |
| `ChatHistory` | PK `token`, SK `ts` | role, content |
| `SystemState` | PK `id`=`hospital` | status (`up`/`down`), snapshot_ts, outage_started_at |

Seed: 3 nurses + 1 charge (Dana) + 2 runners (Marcus, Priya), 12 patients, 3 inventory locations (Unit 4 cabinet, Unit 6 cabinet, Central pharmacy). Include the demo cases: insulin missing from Unit 4 but in Unit 6; heparin nowhere.

## 6. Events (bus: `wardline`)

| DetailType | Source | Consumer | Detail |
|---|---|---|---|
| `OutageDetected` | control | alert_agent, routing_agent | `{started_at}` |
| `MedRequested` | nurse_agent | routing_agent | `{request_id, room, med, requested_by}` |
| `EscalationNeeded` | routing_agent, alert_agent | alert_agent | `{escalation_id, reason, room, to_role}` |
| `AckCheck` | scheduler | alert_agent | `{nurse_id}` |
| `SystemRestored` | control | recovery_agent | `{restored_at}` |

Agents may only publish the events listed for them. No agent publishes an event that re-triggers itself.

## 7. API contract (frontend ↔ backend)

```
POST /chat                 {token, pin, message}
  → {reply, snapshot_ts, brief?: [..], patients: [..], deliveries: [..]}
  (first call with message="" returns the downtime brief)

GET  /dashboard            → {system, staff:[{name, acked}], escalations:[..],
                              deliveries:[..], feed:[{ts, agent, action, detail}]}
  (frontend polls every 3s)

POST /control/outage       → fires OutageDetected
POST /control/restore      → fires SystemRestored
POST /deliveries/{id}/done → marks delivered
```

Salih builds against `VITE_MOCK=1` mock responses matching this contract until the API is live. **Do not change the contract without telling the team.**

## 8. Agents

All agents use `shared/agent_loop.py`:

```python
def run_agent(system_prompt, tools, user_input, tool_impls, max_steps=8):
    # 1. bedrock.converse(modelId, system, messages, toolConfig)
    # 2. if stopReason == "tool_use": run tool_impls[name](**input), append toolResult, loop
    # 3. log every tool call to AgentFeed
    # 4. return final text
```

| Agent | Trigger | Goal | Tools |
|---|---|---|---|
| alert | `OutageDetected`, `AckCheck`, `EscalationNeeded` | Every on-shift nurse reached + acked within 5 min | get_on_shift_staff, get_nurse_workload, send_email, check_ack, schedule_ack_check, escalate |
| nurse | `POST /chat` | Keep this nurse's patients safe | get_my_patients, get_patient(room), log_event, check_recent_doses, request_med, get_delivery_status |
| routing | `MedRequested`, 5-min scan | No dose due in 60 min goes undelivered | get_doses_due, find_stock, get_runners, create_delivery, send_email, escalate |
| recovery | `SystemRestored` | Nothing from downtime is lost | get_downtime_log, get_deliveries, get_escalations, create_report |

Tools bound with server-side context (e.g. `get_my_patients` closes over the verified `nurse_id`). Tools validate inputs (e.g. `get_patient(room)` rejects rooms not assigned to this nurse).

## 9. Ownership

| Person | Owns |
|---|---|
| **Lin — AWS** | `template.yaml` (SAM), DynamoDB tables, EventBridge bus/rules/Scheduler, SES + **verified demo email addresses**, API Gateway + CORS, S3 hosting, IAM roles, Bedrock model access, deploy pipeline. Also `/control` and `/dashboard` Lambdas. |
| **Sunny — Backend (agents core)** | `shared/agent_loop.py`, `shared/events.py`, **routing agent** + tools (`stock.py`, `deliveries.py`), AgentFeed logging, escalation tool |
| **Alisher — Backend (nurse + alerts)** | `data/*.json` + `scripts/seed.py` (first!), `shared/auth.py`, **nurse agent** (`/chat`, brief, log_event, double-dose check, request_med), **alert agent** (send, ack, resend, escalate) |
| **Salih — Frontend** | NurseView (brief, patient cards, chat, delivery status), Dashboard (staff acks, escalations, deliveries board, live agent feed), Control page (2 buttons), mock mode, demo visuals |

Shared: pitch + demo rehearsal (everyone). Whoever finishes first records the backup demo video.

### Checklists

**Phase 1 is fully independent — nobody waits on anyone.** Everyone works against local JSON files, fakes, or mocks first, then switches to real AWS in Phase 2.

#### Lin — AWS
Phase 1 (start now)
- [x] AWS access for all 4 teammates — **shared AWS workshop/event account `580066280520`, region `us-east-1`, via the event's participant login (WSParticipantRole).** No IAM users: the participant role denies `iam:CreateUser`, and the account is event-managed. Everyone uses the same event portal URL + temporary credentials. **Note: temp creds expire — re-login via the event portal when a session lapses. Treat this account as ephemeral (may be reclaimed after the event).**
- [x] Enable Bedrock model access (Claude Haiku) in `us-east-1` — verified via Converse. **Use inference-profile `MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0`** (the bare `anthropic.claude-haiku-4-5-...` ID fails with "on-demand throughput isn't supported" — must use the `us.` inference profile).
- [x] Verify demo email addresses in SES (sandbox) — verified: `lintuwun@gmail.com`, `lintu.wang@sjsu.edu`. **Each nurse/runner demo address must be SES-verified (sandbox) before it can receive mail.** Sender for `send_email` must also be a verified identity (use `lintuwun@gmail.com` as the `FROM_EMAIL`).
- [x] Send one test email via SES — sent gmail → sjsu, SES returned a MessageId (delivered).
- [ ] `template.yaml` skeleton: all DynamoDB tables from §5
- [ ] EventBridge bus `wardline` + rules for the 5 events in §6 (placeholder Lambda targets)
- [ ] API Gateway HTTP API with CORS + placeholder routes from §7
- [ ] S3 bucket with static website hosting
- [ ] `sam deploy` the empty stack successfully
- [ ] Share with team: API URL, table names, region

Phase 2 (after backend pushes code)
- [ ] Wire real Lambdas into SAM (agents, `/chat`, `/dashboard`, `/control`)
- [ ] `/control/outage` + `/control/restore` Lambdas (`put_events` only)
- [ ] `/dashboard` Lambda (reads tables, returns §7 shape)
- [ ] EventBridge Scheduler: 5-min routing scan
- [ ] Deploy frontend build to S3
- [ ] CloudWatch logs; end-to-end flow test
- [ ] (If time) request SES production access to email unverified addresses; else keep all demo addresses verified

#### Sunny — Backend (agent core + routing)
Phase 1 (start now)
- [ ] `shared/agent_loop.py`: Converse tool-use loop with `max_steps` + AgentFeed logging behind an interface
- [ ] Unit test the loop with a **fake Bedrock client** (canned `tool_use` → `end_turn`) — no AWS needed
- [ ] `shared/events.py`: `put_event()` with `DRY_RUN` print mode
- [ ] `tools/stock.py`: `find_stock(med)` reading local `inventory.json` (own 5-line fixture until Alisher's lands)
- [ ] `tools/deliveries.py`: `create_delivery()` with `request_id` dedupe, in-memory store
- [ ] `tools/escalation.py`: `escalate()` → memory + feed
- [ ] `routing_agent/prompt.md`
- [ ] `routing_agent/app.py`: handles `MedRequested` + scan event, runnable locally with fake client

Phase 2 (after Lin's Bedrock access + Alisher's seed data)
- [ ] Swap fake client for real Bedrock; test routing agent live
- [ ] Swap in-memory store for DynamoDB (`shared/db.py`)
- [ ] Demo cases pass: insulin routed from Unit 6; heparin escalated
- [ ] `scripts/reset.py`
- [ ] Stretch: recovery agent

#### Alisher — Backend (nurse agent + alerts)
Phase 1 (start now)
- [ ] `data/nurses.json`, `patients.json`, `inventory.json` per §5 incl. demo cases (insulin missing in Unit 4, heparin nowhere) → **commit within the first hour**
- [ ] `scripts/seed.py` (runs later once tables exist)
- [ ] `shared/auth.py`: token creation + hashed PIN verification, unit tested
- [ ] `tools/patients.py`: `get_my_patients(nurse_id)`, `get_patient(room)` with ownership check, reading JSON
- [ ] `tools/downtime_log.py`: `log_event()` + `check_recent_doses()` (double-dose logic), unit tested
- [ ] `nurse_agent/prompt.md` (start from §12)
- [ ] `shared/notify.py`: `send_email()` (SES) with `NOTIFY_DRY_RUN`
- [ ] Alert agent logic: message builder, ack tracking, resend, escalate — tested with dry-run email

Phase 2 (after Sunny's agent loop + Lin's API)
- [ ] `nurse_agent/app.py` on `run_agent()`: `/chat` returns brief, reply, patients, deliveries
- [ ] `request_med` tool → `MedRequested` event
- [ ] Switch tools from JSON to DynamoDB; run seed
- [ ] Turn off email dry-run; test with real (SES-verified) inboxes
- [ ] Demo cases pass: Kim escalation, double-dose STOP, refuse other nurse's patient

#### Salih — Frontend
Phase 1 (start now)
- [ ] Vite + React app, routes `/nurse?token=`, `/dashboard`, `/control`
- [ ] `api.js` with **mock responses exactly matching §7**, toggled by `VITE_MOCK=1`
- [ ] NurseView: PIN screen, "Records as of" bar, brief card, patient cards, chat, delivery status
- [ ] Dashboard: red/green system status, staff ack list, escalations panel, deliveries board, live agent feed (poll every 3s)
- [ ] Control page: "Simulate ransomware" + "Restore systems" buttons
- [ ] Mobile-first styling, tested at phone width
- [ ] Make the agent feed look impressive — it's the demo centerpiece

Phase 2 (after Lin's API URL)
- [ ] Mock mode off; connect to real API
- [ ] Loading, error, expired-link states
- [ ] Build `dist/` → hand to Lin for S3
- [ ] Record backup demo video

### Cross-team handoffs (the only dependencies)
1. **Alisher → all:** seed JSON in the first hour
2. **Lin → all:** Bedrock access, API URL, table names, verified SES sender address
3. **Sunny → Alisher:** agree on the `run_agent()` signature in the first 15 minutes (before it works)
4. **Salih ↔ backend:** the §7 API contract — nobody changes it without telling the team

## 10. Milestones

**M0 — Setup (first 1–2 hrs)**
- Lin: AWS account access for all, Bedrock model enabled, SES emails verified, empty SAM stack deploys
- Alisher: seed data committed + seeded
- Sunny: `agent_loop.py` calls Bedrock with one dummy tool and returns
- Salih: Vite app with 3 routes on mock data

**M1 — Core loop**
- Nurse agent answers questions over own patients only via `/chat`
- Routing agent handles a manual `MedRequested` → creates delivery → email to runner
- Frontend NurseView talks to real `/chat`

**M2 — Full flow**
- Control button → `OutageDetected` → alert agent emails nurses → links open NurseView
- `request_med` from chat → routing agent → delivery visible in NurseView + Dashboard
- Dashboard live feed polling AgentFeed

**M3 — Demo moments** (must all work)
- Kim doesn't ack → resend → escalation to Dana
- Insulin missing in Unit 4 → routed from Unit 6 → Marcus emailed
- Heparin nowhere → escalation (not a guess)
- Double-dose caught from shared DowntimeLog
- Nurse A asks about Nurse B's patient → refused

**M4 — Polish + freeze**
- `scripts/reset.py` works so the demo can rerun cleanly
- Backup video recorded
- Code freeze 2 hrs before judging; rehearse 3+ times

Stretch: recovery agent report.

## 11. Conventions

- Lambda timeout 60s, memory 512MB
- Times stored ISO 8601 UTC; frontend displays local
- Logging: JSON to CloudWatch; every agent decision ALSO to `AgentFeed`
- `NOTIFY_DRY_RUN=1` env var → log instead of send (use during dev; SES sandbox only sends to verified addresses)
- Branches: `feat/<area>`; PR to `main`; Lin deploys from `main`
- Keep prompts in `backend/agents/<name>/prompt.md`, loaded at runtime
- Small functions, type hints, no frameworks on the backend

## 12. Nurse agent system prompt (core rules)

```
You are Wardline, a calm, brief downtime assistant for one nurse during a hospital system outage.
- You only know the patients provided via tools. They are this nurse's assigned patients.
- Answer only from the records. If something isn't there, say "Not in the record as of {snapshot_ts}."
- Always mention the snapshot time when giving clinical data.
- Never change, recommend, or calculate doses. Never suggest substitutions. Escalate instead.
- Before confirming a med_given log, call check_recent_doses. If already given, say STOP and who/when.
- If a medication is unavailable, call request_med.
- Keep replies under 4 lines. Support clinical judgment; don't replace it.
```

## 13. Demo script (2 min)

1. Hook: real hospitals have run on paper for weeks after ransomware. 2 AM, Dana's unit.
2. Click **Simulate ransomware** → dashboard goes red.
3. Inboxes ping. Kim doesn't ack → escalation to Dana.
4. Nurse opens link → AI brief → allergy question → tries to log a dose already given → **STOP**.
5. "304's insulin isn't in the cabinet" → feed shows routing agent reasoning → Marcus emailed. Heparin → escalated to human.
6. Close: "No one had to notice the attack, call each nurse, or find the medicine. Wardline did — and knew when to hand off to a human."

## 14. Judge Q&A prep

- **Data source when hospital is down?** Isolated AWS backup, one-way sync, versioned snapshots.
- **Attacker reaches backup?** Separate account/credentials; hospital can push but not read/delete.
- **AI wrong call?** Can't change doses or substitute; escalates when unsure; humans approve.
- **Privacy?** Each nurse's agent receives only their patients — enforced in code.
- **Fits real practice?** Follows HICS + downtime procedures; supports the paper MAR; doubles as a drill tool.
- **Production gaps?** EHR/cabinet integration, HIPAA review + BAA, device policies — this is a synthetic-data prototype.
