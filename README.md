# AWS_hackathon

## Wardline (`wardline/`)

Full spec: `wardline/CLAUDE.md`. Tests run from `wardline/` with `python -m pytest` and need no AWS access or boto3.

### Nurse agent + alerts

- `data/*.json`: synthetic seed data with the demo cases (insulin missing in Unit 4 but stocked in Unit 6, heparin nowhere). Staff use `@example.com` addresses, and every account's demo PIN is `1234`. Both are synthetic, not real credentials.
- `scripts/seed.py`: loads `data/` into DynamoDB. Floats such as `temp_f` are converted to Decimal, which boto3 requires.
- `backend/shared/db.py`: DynamoDB helpers and the `DATA_BACKEND` switch.
- `backend/shared/auth.py`: secure-link tokens and PBKDF2 PIN hashes.
- `backend/shared/notify.py`: SES v2 email (replaces `sms.py`, per CLAUDE.md section 3).
- `backend/tools/patients.py`, `backend/tools/downtime_log.py`: patient scoping and the shared dose log.
- `backend/agents/nurse_agent/app.py`: `POST /chat`. `handle_chat(body)` for any server, `api_handler(event)` for API Gateway-style events.
- `backend/agents/alert_agent/app.py`: alert, ack, resend, escalate. Opening the link with a valid PIN counts as the ack.
- Demo cases (Kim escalation, double-dose STOP, refusing another nurse's patient): `backend/tests/test_demo_cases.py`.

### Frontend (`frontend/`)

React + Vite, no other dependencies. Routes: `/nurse?token=…` (nurse view), `/dashboard`, `/control`.

```bash
cd wardline/frontend
npm install
npm run dev:mock      # no backend: answers from src/mock.js (demo PIN 1234, any token)
npm run dev           # live: set VITE_API_URL in .env (see .env.example)
npm run build         # dist/ for the S3 bucket
```

In mock mode, Simulate ransomware on `/control` plays the agents' first half hour on `/dashboard` in about 40 seconds. `?mock-outage=45` on any page starts that outage 45 seconds in, for rehearsals. The demo nurse link is `/nurse?token=demo-n001`.

### Configuration

| Env var | Default | Effect |
|---|---|---|
| `DATA_BACKEND` | `json` | `dynamodb` moves tokens, nurses, patients, the downtime log, alerts and chat history to the section 5 tables |
| `NOTIFY_DRY_RUN` | `1` | `0` sends real email through SES |
| `FROM_EMAIL` | none | SES-verified sender. Required when `NOTIFY_DRY_RUN=0` |
| `NOTIFY_TO_OVERRIDE` | none | Sends every live email to one verified inbox (the seed addresses can't receive mail) |
| `FRONTEND_BASE_URL` | `https://wardline.example/nurse` | Base of the secure link in alert emails |
| `DRY_RUN` | `0` | `1` prints EventBridge events (e.g. `MedRequested`) instead of publishing |
| `MODEL_ID` | none | Bedrock inference profile for `/chat` questions. Without it, chat replies "assistant unavailable"; the brief still works |
| `AWS_REGION` | `us-east-1` | Region for DynamoDB, SES, Bedrock and EventBridge |
| `NURSES_TABLE`, `PATIENTS_TABLE`, ... | section 5 names | Table name overrides |

### Going live

These steps need boto3 and credentials for the workshop account. They haven't been run from this repo yet.

```powershell
pip install boto3
cd wardline
python scripts/seed.py --dry-run      # counts only
python scripts/seed.py                # writes Nurses, Patients, Inventory

$env:DATA_BACKEND = "dynamodb"
$env:NOTIFY_DRY_RUN = "0"
$env:FROM_EMAIL = "<SES-verified sender>"
$env:NOTIFY_TO_OVERRIDE = "<SES-verified inbox>"
python -c "from backend.agents.alert_agent import app; print(app.handler({'detail-type': 'OutageDetected'}))"
```

The last command emails all four on-shift nurses (Dana, Kim, Jordan and Sam). With the override set, every message lands in the one verified inbox.

### Open items outside this workstream

- Routing agent: `get_doses_due` and `get_runners` still use local fixtures that don't match `data/` (heparin in room 312 instead of 602; runners `marcus`/`priya` instead of `N005`/`N006`), and `create_delivery` writes to memory. Until they read the Patients, Nurses and Deliveries tables, a nurse's `request_med` and the delivery it triggers won't share a `request_id`, and `/chat` won't show deliveries with `DATA_BACKEND=dynamodb`. The routing agent's `send_email` is also still a dry-run stub.
- Compute host (CLAUDE.md section 3) is still undecided. `handle_chat` doesn't depend on the host.
- `/chat` has no limit on PIN attempts. The 32-byte link token is the main barrier, but a lockout after a few bad PINs would be worth adding.