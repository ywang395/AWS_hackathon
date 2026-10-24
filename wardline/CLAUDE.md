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

> **✅ COMPUTE DECIDED (team): FastAPI/Flask server on EC2. NO Lambda, NO API Gateway.** The workshop account (`580066280520`) **cannot run Lambda** — IAM is locked down (`iam:CreateRole` and `iam:PassRole` both denied; `WSParticipantRole` is not trusted by the Lambda service). **EC2 is verified working** — a test `t3.micro` was launched to `running` and torn down. So the backend runs as one FastAPI/Flask process on EC2, in the same account as the live DynamoDB/Bedrock/SES/EventBridge. Handlers become server routes; EventBridge-triggered agents become in-process calls + a background scheduler thread. The §7 API contract and the agent/tool code do NOT change — only the host/entry point.
>
> **EC2 gotchas (no IAM roles in this account):** the instance gets **no IAM instance profile** (can't create/pass roles). The server reads AWS credentials from env vars (the participant temp creds) instead — **these expire, so refresh them on the instance when a session lapses.** The public IP changes on stop/start, so don't stop the instance mid-demo. The account is event-ephemeral; keep a **local-run fallback** ready (same server, pointed at the live tables) in case the account is reclaimed.

| Layer | Choice |
|---|---|
| Language (backend) | Python 3.12, boto3 |
| AI | Amazon Bedrock, **Converse API with tool use**, Claude Haiku. `MODEL_ID` env var = **`us.anthropic.claude-haiku-4-5-20251001-v1:0`** (us inference profile; bare model ID is not on-demand invocable) |
| Compute | **Lambda is confirmed blocked** in this account (can't `CreateRole`, can't `PassRole` any Lambda-assumable role, `WSParticipantRole` not trusted by Lambda). **Proposed (pending team decision):** one FastAPI/Flask server on EC2 — single process, agents + API as routes, EventBridge triggers replaced by in-process calls / a background scheduler thread. Agent/tool/handler code unchanged; only the entry point. See §3 banner for alternatives. |
| Events | EventBridge custom bus `wardline` is deployed and used for the live-feed/decoupling story, but agent triggering is **in-process** on the server (no Lambda targets). The 5-min routing scan + ack checks run as a background thread/APScheduler job on the server instead of EventBridge Scheduler. |
| Data | DynamoDB (on-demand) — works in-account, accessed from the server via the `wardline` profile / instance creds |
| Email | Amazon SES v2 `send_email` (sandbox: **verify every demo email address**, both sender and recipients, until production access granted). Replaces SMS — the workshop account has no SMS origination and a non-raisable $1 SMS cap. |
| API | §7 contract unchanged. Host is TBD with the compute decision (proposed: FastAPI/Flask routes on the chosen server, CORS enabled — was API Gateway HTTP API, which is not used since there are no Lambdas to integrate). |
| Frontend | React + Vite (static build), hosted on S3 static website; points at the backend server URL (set once compute is decided) |
| IaC | AWS SAM `template.yaml` currently deploys the **data plane only** (DynamoDB tables, EventBridge bus, S3) — this part is done and stable regardless of the compute decision. |
| Region | `us-east-1` (everything in ONE region with Bedrock model access enabled) |

## 4. Repo structure

```
wardline/
├── CLAUDE.md
├── template.yaml                 # SAM: data plane only (tables, bus, S3). No Lambdas/API (see §3).
├── backend/
│   ├── server.py                 # (PROPOSED) FastAPI/Flask entry point mounting §7 routes + scheduler — only if the team picks the server option (§3)
│   ├── shared/
│   │   ├── agent_loop.py         # Bedrock Converse tool-use loop (shared by all agents)
│   │   ├── db.py                 # DynamoDB helpers
│   │   ├── events.py             # put_event() helper
│   │   ├── notify.py             # SES send_email helper (+ DRY_RUN mode)  [formerly sms.py]
│   │   └── auth.py               # token + PIN verification
│   ├── agents/                   # handler fns (invoked by whatever compute host the team picks — §3)
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
| **Lin — AWS** | `template.yaml` (data plane: DynamoDB tables, EventBridge bus, S3), SES + **verified demo email addresses**, Bedrock model access, S3 hosting, deploy pipeline, **and the backend compute host once the team decides it** (proposed: EC2 + `server.py`). Also the `/control` and `/dashboard` route handlers. **IAM:** no IAM users, no new/usable IAM roles — `WSParticipantRole` denies `iam:CreateUser`, `iam:CreateRole`, and `iam:PassRole`, and isn't trusted by Lambda. Lambda is therefore off the table; compute approach is an open team decision (see §3). |
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
- [x] `template.yaml` skeleton: all DynamoDB tables from §5
- [x] EventBridge bus `wardline` + rules for the 5 events in §6 (targets added in Phase 2 — see note below)
- [x] API Gateway HTTP API with CORS (routes wired in Phase 2 — see note below)
- [x] S3 bucket with static website hosting
- [x] Deploy the empty stack successfully — **deployed via `aws cloudformation deploy`** (not `sam` CLI; see note). Stack name `wardline`.
- [x] Share with team: API URL, table names, region (see **Deployed resources** below)

> **IAM constraint (workshop account):** `WSParticipantRole` denies `iam:CreateRole`, so the Phase 1 stack creates **no Lambdas** (every Lambda needs an execution role). EventBridge rules and the HTTP API are deployed **without** Lambda targets/integrations; those are wired in Phase 2 once real agent code exists and an execution role is sorted. Deploy with `aws cloudformation deploy --template-file template.yaml --stack-name wardline --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND --region us-east-1` (the Homebrew `sam` CLI is broken on this machine — Python 3.15/libexpat mismatch).

> **Deployed resources (share these):**
> - Region: `us-east-1`
> - API base URL: `https://6haxdip6xj.execute-api.us-east-1.amazonaws.com`
> - EventBridge bus: `wardline`
> - DynamoDB tables: `Nurses`, `Patients`, `Inventory`, `Deliveries`, `DowntimeLog`, `Tokens`, `Alerts`, `Escalations`, `AgentFeed`, `ChatHistory`, `SystemState`
> - Frontend S3 bucket: `wardline-frontend-580066280520`
> - Frontend URL: `http://wardline-frontend-580066280520.s3-website-us-east-1.amazonaws.com`
> - SES verified sender: `lintuwun@gmail.com` (recipients must also be SES-verified while in sandbox)

Phase 2 (after backend pushes code) — **compute = FastAPI/Flask on EC2 (§3). Lambda is NOT available.**
- [x] **Compute decided: EC2 server** (Lambda impossible; EC2 launch verified). See §3 banner.
- [x] Switch tools JSON → DynamoDB + seed live tables — done via `scripts/go_live_dynamodb.py`; read-back checks all pass (12 nurses / 16 patients / 25 inventory live in `us-east-1`).
- [ ] Launch EC2 (t3.small, default VPC `vpc-08bb470f1b40c6636`, subnet `subnet-0297a1b1ba5f29a20`, Amazon Linux 2023 `ami-08c46876c64a927b8`), SG open for the server port (+ SSH or SSM)
- [ ] `backend/server.py`: one FastAPI/Flask app exposing the §7 routes (`/chat`, `/dashboard`, `/control/outage`, `/control/restore`, `/deliveries/{id}/done`), CORS enabled
- [ ] Mount backend handlers as routes — same code, server entry point instead of Lambda handler
- [ ] Replace EventBridge-triggered agents with in-process calls; run the 5-min routing scan + ack checks as a background scheduler thread (APScheduler)
- [ ] `/control/outage` + `/control/restore` → put_events on the `wardline` bus (kept for the feed/story) AND trigger the agent in-process
- [ ] `/dashboard` route (reads tables, returns §7 shape)
- [ ] Credentials on EC2: participant temp creds as env vars (no instance profile — see §3 gotchas); refresh on expiry
- [ ] Deploy frontend build to S3; point `VITE_API_URL` at the EC2 public URL
- [ ] Logs (server stdout); end-to-end flow test
- [ ] Keep a local-run fallback (same server, live tables) in case the account is reclaimed
- [ ] (If time) request SES production access to email unverified addresses; else keep all demo addresses verified

#### Sunny — Backend (agent core + routing)
Phase 1 (start now)
- [x] `shared/agent_loop.py`: Converse tool-use loop with `max_steps` + AgentFeed logging behind an interface
- [x] Unit test the loop with a **fake Bedrock client** (canned `tool_use` → `end_turn`) — no AWS needed
- [x] `shared/events.py`: `put_event()` with `DRY_RUN` print mode
- [x] `tools/stock.py`: `find_stock(med)` reading local `inventory.json` (own 5-line fixture until Alisher's lands)
- [x] `tools/deliveries.py`: `create_delivery()` with `request_id` dedupe, in-memory store
- [x] `tools/escalation.py`: `escalate()` → memory + feed
- [x] `routing_agent/prompt.md`
- [x] `routing_agent/app.py`: handles `MedRequested` + scan event, runnable locally with fake client

Phase 2 (after Lin's Bedrock access + Alisher's seed data)
- [x] Swap fake client for real Bedrock; test routing agent live (`python -m backend.agents.routing_agent.cli --live --case insulin|heparin|scan|outage`; opt-in `tests/test_routing_live.py` with `WARDLINE_LIVE_BEDROCK=1`)
- [ ] Swap in-memory store for DynamoDB (`shared/db.py`) — AgentFeed writer done (`DynamoAgentFeed`, auto-used when `AGENT_FEED_TABLE` is set); Deliveries/Escalations/Inventory still in-memory/JSON
- [x] Demo cases pass on live Bedrock + seed data: insulin (room 304) routed from Unit 6; heparin (room 602) escalated + `EscalationNeeded` published
- [ ] `scripts/reset.py`
- [ ] Stretch: recovery agent

#### Alisher — Backend (nurse agent + alerts)
Phase 1 (start now)
- [x] `data/nurses.json`, `patients.json`, `inventory.json` per §5 incl. demo cases (insulin missing in Unit 4, heparin nowhere) → **commit within the first hour**
- [x] `scripts/seed.py` (runs later once tables exist)
- [x] `shared/auth.py`: token creation + hashed PIN verification, unit tested
- [x] `tools/patients.py`: `get_my_patients(nurse_id)`, `get_patient(room)` with ownership check, reading JSON
- [x] `tools/downtime_log.py`: `log_event()` + `check_recent_doses()` (double-dose logic), unit tested
- [x] `nurse_agent/prompt.md` (start from §12)
- [x] `shared/sms.py`: `send_sms()` with `SMS_DRY_RUN`
- [x] Alert agent logic: message builder, ack tracking, resend, escalate — tested with dry-run SMS

Phase 2 (after Sunny's agent loop + Lin's API)
- [x] `nurse_agent/app.py` on `run_agent()`: `/chat` returns brief, reply, patients, deliveries
- [x] `request_med` tool → `MedRequested` event
- [ ] Switch tools from JSON to DynamoDB; run seed
  - Code ready: tools read DynamoDB when `DATA_BACKEND=dynamodb` (`shared/db.py`); passes against the in-memory fake (`test_dynamodb_backend.py`). Go-live: `python scripts/go_live_dynamodb.py` (seeds + reads back). Awaits a run on the workshop account (boto3 + credentials + deployed tables).
- [ ] Turn off email dry-run; test with real (SES-verified) inboxes
  - Code ready: `shared/notify.py` sends via SES v2; send path passes with a fake client (`test_go_live_scripts.py`). Go-live: set `NOTIFY_TO_OVERRIDE` to a verified inbox in `scripts/live_config.py`, then `python scripts/go_live_email.py`. Awaits a run on the workshop account (boto3 + SES send perms).
- [x] Demo cases pass: Kim escalation, double-dose STOP, refuse other nurse's patient

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
