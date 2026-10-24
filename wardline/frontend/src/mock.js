// The section 7 API, answered in the browser for VITE_MOCK=1 / `npm run dev:mock`.
//
// Shapes copy the backend: server.py (/dashboard, /control), nurse_agent/app.py (/chat and its
// brief), tools/deliveries.py, tools/escalation.py and shared/agent_loop.py (feed rows). The
// patients are nurse N001's seed records from data/patients.json, with their times moved so the
// night starts when the outage does. The night is compressed: after "Simulate ransomware" the
// agents' first half hour plays out over about 40 seconds.
//
// State lives in localStorage so the control page, the dashboard and the nurse view (separate
// tabs) see one hospital. Demo PIN: 1234 with any token, like the seed accounts.

const KEY = 'wardline-mock-v1'
const DEMO_PIN = '1234'
const SEED_OUTAGE = Date.parse('2024-01-15T02:10:00Z')
const ME = 'N001'

const STAFF = [
  { nurse_id: 'N001', name: 'Lin Wang', acksAfter: null }, // acks by opening the nurse view
  { nurse_id: 'N002', name: 'Alisher Nurmatov', acksAfter: 6 },
  { nurse_id: 'N003', name: 'Salih Algosaibi', acksAfter: Infinity }, // never: the escalation case
  { nurse_id: 'N004', name: 'Sunny Lam', acksAfter: 11 },
]

const PATIENTS = [
  {
    patient_id: 'P301', assigned_nurse_id: 'N001', room: '301', name: 'Beatrice Howell', dob: '1939-12-05', allergies: [],
    meds: [{ med: 'Warfarin', dose: '3mg', route: 'PO', due_at: '2024-01-15T03:30:00Z', critical: true }],
    vitals: { hr: 74, bp: '118/72', spo2: 96, temp_f: 97.8, recorded_at: '2024-01-15T02:05:00Z' },
    notes: 'Atrial fibrillation on anticoagulation. INR pending; hold if active bleeding. MODERATE.',
    snapshot_ts: '2024-01-15T02:05:00Z',
  },
  {
    patient_id: 'P302', assigned_nurse_id: 'N001', room: '302', name: 'Travis Nakamura', dob: '1990-06-14', allergies: [],
    meds: [{ med: 'Acetaminophen', dose: '650mg', route: 'PO', due_at: '2024-01-15T02:45:00Z', critical: false }],
    vitals: { hr: 72, bp: '120/78', spo2: 99, temp_f: 99.0, recorded_at: '2024-01-15T02:05:00Z' },
    notes: 'Observation after minor concussion. Pain 3/10. MINOR.',
    snapshot_ts: '2024-01-15T02:05:00Z',
  },
  {
    patient_id: 'P303', assigned_nurse_id: 'N001', room: '303', name: 'Gloria Mendez', dob: '1955-02-28', allergies: ['Sulfa drugs'],
    meds: [{ med: 'Furosemide', dose: '40mg', route: 'IV', due_at: '2024-01-15T04:15:00Z', critical: false }],
    vitals: { hr: 88, bp: '146/90', spo2: 95, temp_f: 98.6, recorded_at: '2024-01-15T02:05:00Z' },
    notes: 'CHF, stable tonight. Strict I/O. MODERATE.',
    snapshot_ts: '2024-01-15T02:05:00Z',
  },
  {
    patient_id: 'P304', assigned_nurse_id: 'N001', room: '304', name: 'Eleanor Whitmore', dob: '1947-03-18', allergies: ['Penicillin'],
    meds: [{ med: 'Insulin', dose: '8 units', route: 'SubQ', due_at: '2024-01-15T02:20:00Z', critical: true }],
    vitals: { hr: 96, bp: '148/92', spo2: 94, temp_f: 99.4, recorded_at: '2024-01-15T02:05:00Z' },
    notes: 'Type 1 diabetic, DKA risk. Insulin NOT in Unit 4 cabinet at downtime; blood glucose 312. SEVERE - route from Unit 6.',
    snapshot_ts: '2024-01-15T02:05:00Z',
  },
]

// ---------------------------------------------------------------------------
// State and clock
// ---------------------------------------------------------------------------

let memory = null

const fresh = () => ({ status: 'up', outageAt: null, restoredAt: null, anchor: Date.now(), acked: false, chat: [], logged: [], delivered: {}, requested: [] })

function load() {
  try {
    const raw = localStorage.getItem(KEY)
    if (raw) return JSON.parse(raw)
  } catch { /* storage blocked: fall back to memory */ }
  return memory || (memory = fresh())
}

function save(state) {
  memory = state
  try { localStorage.setItem(KEY, JSON.stringify(state)) } catch { /* memory only */ }
}

const iso = (ms) => new Date(ms).toISOString()
const isoSeconds = (ms) => iso(ms).replace(/\.\d{3}Z$/, 'Z')
const utc = (ts) => `${new Date(ts).toISOString().slice(11, 16)} UTC`
const base = (s) => s.outageAt || s.anchor
// A seed time ("02:20 on the seed night") moved onto tonight's clock.
const at = (s, seedIso) => isoSeconds(base(s) + Date.parse(seedIso) - SEED_OUTAGE)
const elapsed = (s) => (s.outageAt ? ((s.restoredAt || Date.now()) - s.outageAt) / 1000 : -1)

function patients(s) {
  return PATIENTS.map((p) => ({
    ...p,
    meds: p.meds.map((m) => ({ ...m, due_at: at(s, m.due_at) })),
    vitals: { ...p.vitals, recorded_at: at(s, p.vitals.recorded_at) },
    snapshot_ts: at(s, p.snapshot_ts),
  }))
}

const requestId = (room, med, dueAt) => `${room}-${med.toLowerCase()}-${dueAt}`

// ---------------------------------------------------------------------------
// The scripted night: routing scan, deliveries, escalations (offsets in seconds)
// ---------------------------------------------------------------------------

function script(s) {
  const t = (sec) => iso(s.outageAt + sec * 1000)
  const insulinDue = at(s, '2024-01-15T02:20:00Z')
  const heparinDue = at(s, '2024-01-15T02:40:00Z')
  const delivery = {
    request_id: requestId('304', 'Insulin', insulinDue), med: 'Insulin', from_location: 'Unit 6 cabinet', to_room: '304',
    due_at: insulinDue, priority: 'critical', runner_id: 'N005', status: 'assigned', created_at: isoSeconds(s.outageAt + 9000),
  }
  const heparin = {
    escalation_id: 'b7d4c2e0-5f1a-4c8e-9a61-3e2f0d9c6a11', source_agent: 'routing_agent',
    reason: `Heparin 5000 units SubQ for room 602 is not in stock in any cabinet or in Central Pharmacy (due ${utc(heparinDue)}). A pharmacist or the charge nurse must decide; no substitution was made.`,
    room: '602', nurse_id: null, to_role: 'charge', created_at: isoSeconds(s.outageAt + 12200), resolved: false,
  }
  const salih = {
    escalation_id: 'e19a0f6b-2c3d-4b7e-8f50-6a1d9c2b7e44', source_agent: 'alert_agent',
    reason: `Salih Algosaibi hasn’t confirmed after 2 emails. Rooms 309–312 have meds due from ${utc(at(s, '2024-01-15T04:30:00Z'))}. Please check on them.`,
    room: '', nurse_id: 'N003', to_role: 'charge', created_at: isoSeconds(s.outageAt + 30000), resolved: false,
  }
  const call = (sec, id, tool, input, result, step) => [
    { at: sec, ts: t(sec), agent: 'routing_agent', action: 'tool_requested', detail: { step, tool_use_id: id, tool, input } },
    { at: sec + 0.6, ts: t(sec + 0.6), agent: 'routing_agent', action: 'tool_completed', detail: { step, tool_use_id: id, tool, result } },
  ]
  const feed = [
    { at: 2, ts: t(2), agent: 'routing_agent', action: 'agent_started', detail: { model_id: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', max_steps: 8, tool_count: 6 } },
    ...call(3, 'tu-r1', 'get_doses_due', {}, {
      snapshot_ts: at(s, '2024-01-15T02:05:00Z'), window_end: at(s, '2024-01-15T03:05:00Z'),
      doses: [
        { room: '304', med: 'Insulin', dose: '8 units', due_at: insulinDue, critical: true },
        { room: '602', med: 'Heparin', dose: '5000 units', due_at: heparinDue, critical: true },
        { room: '302', med: 'Acetaminophen', dose: '650mg', due_at: at(s, '2024-01-15T02:45:00Z'), critical: false },
      ],
    }, 1),
    ...call(5, 'tu-r2', 'find_stock', { med: 'Insulin' }, [{ location: 'Unit 6 cabinet', med: 'Insulin', qty: 15, distance_rank: 1 }], 2),
    ...call(7, 'tu-r3', 'get_runners', {}, [
      { runner_id: 'N005', name: 'Marcus Webb', location: 'Central Pharmacy', workload: 0 },
      { runner_id: 'N006', name: 'Priya Nair', location: 'Central Pharmacy', workload: 0 },
    ], 3),
    ...call(8.4, 'tu-r4', 'create_delivery', { room: '304', med: 'Insulin', due_at: insulinDue, from_location: 'Unit 6 cabinet', priority: 'critical', runner_id: 'N005' }, delivery, 4),
    ...call(9.6, 'tu-r5', 'send_email', { runner_id: 'N005', subject: `URGENT: Insulin, Unit 6 cabinet → Room 304 by ${utc(insulinDue)}`, body: 'Critical dose. Collect from the Unit 6 cabinet.' },
      { status: 'sent', runner_id: 'N005', subject: `URGENT: Insulin, Unit 6 cabinet → Room 304 by ${utc(insulinDue)}`, message_id: '0100019a-mock' }, 5),
    ...call(10.8, 'tu-r6', 'find_stock', { med: 'Heparin' }, [], 6),
    { at: 12, ts: t(12), agent: 'routing_agent', action: 'tool_requested', detail: { step: 7, tool_use_id: 'tu-r7', tool: 'escalate', input: { reason: heparin.reason, room: '602', to_role: 'charge', med: 'Heparin' } } },
    { at: 12.2, ts: t(12.2), agent: 'routing_agent', action: 'escalation_created', detail: heparin },
    { at: 12.4, ts: t(12.4), agent: 'routing_agent', action: 'event_published', detail: { detail_type: 'EscalationNeeded', escalation_id: heparin.escalation_id, reason: heparin.reason, room: '602', to_role: 'charge' } },
    { at: 12.6, ts: t(12.6), agent: 'routing_agent', action: 'tool_completed', detail: { step: 7, tool_use_id: 'tu-r7', tool: 'escalate', result: { ...heparin, event_published: true } } },
    { at: 14, ts: t(14), agent: 'routing_agent', action: 'agent_completed', detail: { steps: 8, stop_reason: 'end_turn',
      reply: `Insulin for room 304 is coming from the Unit 6 cabinet with Marcus Webb, due ${utc(insulinDue)}. Heparin for room 602 is not in stock anywhere, so I escalated it to the charge nurse. Acetaminophen for room 302 is in the Unit 4 cabinet.` } },
    { at: 30, ts: t(30), agent: 'alert_agent', action: 'escalation_created', detail: salih },
  ]
  return { feed, delivery, escalations: [{ at: 12.2, ...heparin }, { at: 30, ...salih }] }
}

function deliveries(s) {
  const now = elapsed(s)
  const out = []
  if (s.outageAt && now >= 9) {
    const { delivery } = script(s)
    out.push({ ...delivery, status: now >= 25 ? 'picked_up' : 'assigned' })
  }
  for (const d of s.requested) if (!out.some((o) => o.request_id === d.request_id)) out.push(d)
  return out.map((d) => (s.delivered[d.request_id] ? { ...d, status: 'delivered' } : d))
}

// ---------------------------------------------------------------------------
// GET /dashboard, POST /control/*, POST /deliveries/{id}/done
// ---------------------------------------------------------------------------

function dashboard() {
  const s = load()
  const now = elapsed(s)
  const scripted = s.outageAt ? script(s) : { feed: [], escalations: [] }
  const feed = [...scripted.feed.filter((e) => e.at <= now), ...s.chat]
    .map(({ ts, agent, action, detail }) => ({ ts, agent, action, detail }))
    .sort((a, b) => b.ts.localeCompare(a.ts))
    .slice(0, 50)
  return {
    system: {
      status: s.status,
      snapshot_ts: s.outageAt ? at(s, '2024-01-15T02:05:00Z') : null,
      outage_started_at: s.status === 'down' ? isoSeconds(s.outageAt) : null,
    },
    staff: STAFF.map((n) => ({
      name: n.name,
      acked: Boolean(s.outageAt) && (n.nurse_id === ME ? s.acked : now >= n.acksAfter),
    })),
    escalations: scripted.escalations.filter((e) => e.at <= now).map(({ at: _at, ...e }) => e),
    deliveries: deliveries(s),
    feed,
  }
}

function outage() {
  const s = { ...fresh(), status: 'down', outageAt: Date.now() }
  save(s)
  return {
    status: 'down',
    outage_started_at: isoSeconds(s.outageAt),
    alerts: STAFF.map((n) => ({ nurse_id: n.nurse_id, action: 'sent', attempt: 1 })),
    routing: { event_type: 'OutageDetected', coverage: { retried: [], escalated: [] } },
  }
}

function restore() {
  const s = load()
  const restoredAt = Date.now()
  save({ ...s, status: 'up', restoredAt: s.outageAt ? restoredAt : null })
  return { status: 'up', restored_at: isoSeconds(restoredAt) }
}

function done(id) {
  const s = load()
  const d = deliveries(s).find((x) => x.request_id === id)
  if (!d) return [404, { error: `Delivery '${id}' not found: unknown request_id` }]
  s.delivered[id] = Date.now()
  save(s)
  return [200, { ...d, status: 'delivered' }]
}

// ---------------------------------------------------------------------------
// POST /chat: the brief, then canned nurse-agent answers that also write feed runs
// ---------------------------------------------------------------------------

function brief(list) {
  return [...list].sort((a, b) => a.room.localeCompare(b.room)).map((p) => {
    const meds = [...p.meds].sort((a, b) => a.due_at.localeCompare(b.due_at))
    return {
      room: p.room, patient_id: p.patient_id, name: p.name, allergies: [...p.allergies],
      next_due: meds[0] || null, critical_meds: meds.filter((m) => m.critical).map((m) => m.med), notes: p.notes,
    }
  })
}

function priorInsulin(s) {
  const ts = Math.min(base(s) + 25_000, Date.now() - 60_000)
  return { patient_id: 'P304', med: 'Insulin', room: '304', given_by: 'Alisher Nurmatov', nurse_id: 'N002', ts: isoSeconds(ts), detail: 'Insulin 8 units SubQ', double_checked_by: 'Sunny Lam' }
}

function answer(message, s, list) {
  const text = message.toLowerCase()
  const roomMatch = text.match(/\b(\d{3})\b/)
  const room = roomMatch?.[1]
  const mine = (r) => list.find((p) => p.room === r)
  const named = list.find((p) => p.meds.some((m) => text.includes(m.med.toLowerCase())))
  const patient = (room && mine(room)) || named
  const med = patient?.meds.find((m) => text.includes(m.med.toLowerCase())) || patient?.meds[0]
  const now = Date.now()
  const steps = []

  if (room && !mine(room)) {
    steps.push(['get_patient', { room }, undefined, 'PatientAccessDenied'])
    return { reply: `Room ${room} isn’t one of your patients, so I can’t open that record. Ask the charge nurse.`, steps }
  }

  if (/(gave|given|give|administer|logg?(ed|ing)?\b)/.test(text) && patient && med) {
    const history = [priorInsulin(s), ...s.logged].filter((l) => l.room === patient.room && l.med === med.med)
    const recent = history.map((l) => ({ given_by: l.given_by, nurse_id: l.nurse_id, ts: l.ts, detail: l.detail, double_checked_by: l.double_checked_by }))
    steps.push(['check_recent_doses', { room: patient.room, med: med.med }, recent])
    if (recent.length) {
      const last = recent[recent.length - 1]
      steps.push(['log_event', { room: patient.room, event_type: 'med_given', detail: `${med.med} ${med.dose} ${med.route}` },
        { status: 'stop', med: med.med, room: patient.room, ...last, message: `STOP: ${med.med} was already given to room ${patient.room} by ${last.given_by} at ${last.ts}.` }])
      const checked = last.double_checked_by ? ` (double-checked by ${last.double_checked_by})` : ''
      return { reply: `STOP: ${med.med} was already given to room ${patient.room} by ${last.given_by} at ${utc(last.ts)}${checked}. Don’t give it again. Check with ${last.given_by} or the charge nurse.`, steps }
    }
    const entry = { patient_id: patient.patient_id, med: med.med, room: patient.room, given_by: 'Lin Wang', nurse_id: ME, ts: isoSeconds(now), detail: `${med.med} ${med.dose} ${med.route}`, double_checked_by: null }
    s.logged.push(entry)
    steps.push(['log_event', { room: patient.room, event_type: 'med_given', detail: entry.detail }, { status: 'logged', room: patient.room, ...entry }])
    return { reply: `Logged: ${med.med} ${med.dose} ${med.route} given to room ${patient.room} at ${utc(now)}. The paper MAR stays the official record.`, steps }
  }

  if (/(not in|isn.t in|missing|out of|can.?t find|cannot find|none left|empty)/.test(text) && patient && med) {
    const id = requestId(patient.room, med.med, med.due_at)
    steps.push(['request_med', { room: patient.room, med: med.med }, { status: 'requested', due_at: med.due_at, request_id: id, room: patient.room, med: med.med, requested_by: ME }])
    if (!deliveries(s).some((d) => d.request_id === id)) {
      s.requested.push({ request_id: id, med: med.med, from_location: med.med === 'Insulin' ? 'Unit 6 cabinet' : 'Central Pharmacy', to_room: patient.room,
        due_at: med.due_at, priority: med.critical ? 'critical' : 'routine', runner_id: 'N005', status: 'assigned', created_at: isoSeconds(now) })
    }
    const from = deliveries(s).find((d) => d.request_id === id)?.from_location || 'the nearest stock'
    return { reply: `Requested. ${med.med} for room ${patient.room} is coming from ${from} with Marcus Webb, due ${utc(med.due_at)}.`, steps }
  }

  if (/allerg/.test(text)) {
    steps.push(['get_my_patients', {}, list])
    const withAllergies = list.filter((p) => p.allergies.length)
    const none = list.filter((p) => !p.allergies.length).map((p) => p.room)
    const lines = withAllergies.map((p) => `Room ${p.room}, ${p.name}: ${p.allergies.join(', ')}.`)
    if (none.length) lines.push(`No known allergies: rooms ${none.join(' and ')}.`)
    return { reply: lines.join('\n'), steps }
  }

  if (/(due|next hour|schedule|coming up)/.test(text)) {
    steps.push(['get_my_patients', {}, list])
    const soon = list.flatMap((p) => p.meds.map((m) => ({ ...m, room: p.room })))
      .filter((m) => Date.parse(m.due_at) - now <= 60 * 60_000)
      .sort((a, b) => a.due_at.localeCompare(b.due_at))
    if (!soon.length) return { reply: 'Nothing is due in the next hour.', steps }
    return { reply: ['Due in the next hour:', ...soon.map((m) => `• ${utc(m.due_at)} room ${m.room}: ${m.med} ${m.dose} ${m.route}${m.critical ? ' (critical)' : ''}`)].join('\n'), steps }
  }

  if (patient) {
    steps.push(['get_patient', { room: patient.room }, patient])
    const allergy = patient.allergies.length ? `Allergies: ${patient.allergies.join(', ')}.` : 'No known allergies.'
    const v = patient.vitals
    return { reply: `Room ${patient.room}, ${patient.name}. ${allergy} ${med.med} ${med.dose} ${med.route} due ${utc(med.due_at)}. Last vitals ${utc(v.recorded_at)}: HR ${v.hr}, BP ${v.bp}, SpO2 ${v.spo2}%.`, steps }
  }

  return { reply: `Not in the record as of ${utc(list[0].snapshot_ts)}.`, steps }
}

// A nurse_agent run in the feed, shaped like shared/agent_loop.py writes it.
function runEvents(steps, reply) {
  let ms = Date.now()
  const next = () => iso((ms += 180))
  const events = [{ ts: next(), agent: 'nurse_agent', action: 'agent_started', detail: { model_id: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', max_steps: 8, tool_count: 6 } }]
  steps.forEach(([tool, input, result, error], i) => {
    const id = `tu-n${ms}-${i}`
    events.push({ ts: next(), agent: 'nurse_agent', action: 'tool_requested', detail: { step: i + 1, tool_use_id: id, tool, input } })
    events.push(error
      ? { ts: next(), agent: 'nurse_agent', action: 'tool_failed', detail: { step: i + 1, tool_use_id: id, tool, error_type: error, error: error } }
      : { ts: next(), agent: 'nurse_agent', action: 'tool_completed', detail: { step: i + 1, tool_use_id: id, tool, result } })
  })
  events.push({ ts: next(), agent: 'nurse_agent', action: 'agent_completed', detail: { steps: steps.length + 1, stop_reason: 'end_turn', reply } })
  return events
}

function chat(body) {
  const { token, pin, message = '' } = body || {}
  if (typeof token !== 'string' || !token.trim() || typeof pin !== 'string' || !pin.trim()) return [400, { error: 'token and pin are required strings.' }]
  if (typeof message !== 'string') return [400, { error: 'message must be a string.' }]
  if (pin.trim() !== DEMO_PIN) return [401, { error: 'Invalid link or PIN.' }]

  const s = load()
  if (s.outageAt) s.acked = true // opening the link with a valid PIN is the ack
  const list = patients(s)
  const snapshot = list.map((p) => p.snapshot_ts).sort().at(-1)
  const shown = utc(snapshot)
  const rooms = new Set(list.map((p) => p.room))
  const response = { reply: '', snapshot_ts: snapshot, patients: list }

  if (!message.trim()) {
    save(s)
    return [200, { ...response, deliveries: deliveries(s).filter((d) => rooms.has(d.to_room)), brief: brief(list),
      reply: `Downtime brief: ${list.length} patient(s) assigned to you. Records as of ${shown}.` }]
  }

  const { reply, steps } = answer(message, s, list)
  const final = reply.includes(shown) ? reply : `${reply}\n(Records as of ${shown})`
  s.chat.push(...runEvents(steps, final))
  save(s)
  return [200, { ...response, deliveries: deliveries(s).filter((d) => rooms.has(d.to_room)), reply: final }]
}

// ---------------------------------------------------------------------------

// Rehearsal and screenshots: ?mock-outage=45 starts a fresh outage 45 seconds ago, so the page
// opens with the night already under way. Mock mode only.
const jump = new URLSearchParams(globalThis.location?.search || '').get('mock-outage')
if (jump !== null) {
  const ago = Math.max(0, Number(jump) || 0) * 1000
  save({ ...fresh(), status: 'down', outageAt: Date.now() - ago, anchor: Date.now() - ago })
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms))

export async function mockRequest(method, path, body) {
  const route = `${method} ${path}`
  let status = 200
  let out
  if (route === 'POST /chat') {
    await wait(body?.message ? 900 : 450)
    ;[status, out] = chat(body)
  } else if (route === 'GET /dashboard') {
    await wait(150)
    out = dashboard()
  } else if (route === 'POST /control/outage') {
    await wait(600)
    out = outage()
  } else if (route === 'POST /control/restore') {
    await wait(300)
    out = restore()
  } else if (method === 'POST' && /^\/deliveries\/[^/]+\/done$/.test(path)) {
    await wait(250)
    ;[status, out] = done(decodeURIComponent(path.split('/')[2]))
  } else {
    status = 404
    out = { error: 'Not Found' }
  }
  return { status, body: JSON.parse(JSON.stringify(out)) }
}
