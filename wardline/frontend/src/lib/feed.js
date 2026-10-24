// Turns AgentFeed rows ({ts, agent, action, detail}, newest first) into runs a person can read.
//
// shared/agent_loop.py writes agent_started, tool_requested, tool_completed / tool_failed and
// agent_completed (or step_limit_reached) for every run; code paths add escalation_created
// (tools/escalation.py), event_published, scan_dose_unhandled and agent_failed (routing agent).
// A run groups one agent's events from agent_started to its end. Events outside a run, or whose
// agent_started fell outside the 50-row window, stand alone.

import { clock, DELIVERY_STATUS, plural, runnerName, stripMarkdown } from './format.js'

const AGENTS = {
  routing_agent: 'Routing agent',
  nurse_agent: 'Nurse agent',
  alert_agent: 'Alert agent',
  recovery_agent: 'Recovery agent',
}

export const agentLabel = (agent) => AGENTS[agent] || humanize(agent || 'agent')

const humanize = (s) => String(s).replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())

const ENDS = { agent_completed: 'done', step_limit_reached: 'failed', agent_failed: 'failed' }

export function groupRuns(feed = []) {
  const events = [...feed].sort((a, b) => String(a.ts).localeCompare(String(b.ts)))
  const runs = []
  const open = new Map()
  for (const event of events) {
    const { agent, action, detail = {} } = event
    if (action === 'agent_started') {
      const run = { id: `${agent}-${event.ts}`, agent, started: event.ts, ended: null, status: 'running', steps: [], reply: '', note: '' }
      runs.push(run)
      open.set(agent, run)
      continue
    }
    const run = open.get(agent)
    if (!run) {
      runs.push({ id: `${agent}-${event.ts}-${action}`, agent, started: event.ts, ended: event.ts, status: 'single', steps: [{ id: event.ts, event }] })
      continue
    }
    if (action === 'tool_requested') {
      run.steps.push({ id: detail.tool_use_id || event.ts, tool: detail.tool, input: detail.input || {}, ts: event.ts })
    } else if (action === 'tool_completed' || action === 'tool_failed') {
      const step = run.steps.find((s) => s.tool && s.id === detail.tool_use_id) || run.steps.findLast((s) => s.tool === detail.tool)
      const target = step || { id: detail.tool_use_id || event.ts, tool: detail.tool, input: {}, ts: event.ts }
      if (!step) run.steps.push(target)
      if (action === 'tool_completed') target.result = detail.result
      else target.error = detail.error || detail.error_type || 'failed'
      target.done = true
    } else if (ENDS[action]) {
      run.status = ENDS[action]
      run.ended = event.ts
      run.reply = detail.reply || ''
      run.note = action === 'agent_completed' ? '' : action === 'step_limit_reached' ? 'Stopped at its step limit' : `Failed (${detail.error_type || 'error'})`
      open.delete(agent)
    } else {
      run.steps.push({ id: event.ts, event })
    }
  }
  for (const run of runs) {
    // event_published is plumbing (the bus), not a decision a nurse needs to read.
    run.steps = run.steps.filter((s) => s.event?.action !== 'event_published')
    // The escalate tool step already says what escalation_created says.
    if (run.steps.some((s) => s.tool === 'escalate')) {
      run.steps = run.steps.filter((s) => s.event?.action !== 'escalation_created')
    }
  }
  return runs.reverse()
}

// Runner names learned from get_runners results, so deliveries can show "Marcus Webb".
export function runnerNames(feed = []) {
  const names = {}
  for (const { action, detail } of feed) {
    if (action === 'tool_completed' && detail?.tool === 'get_runners' && Array.isArray(detail.result)) {
      for (const r of detail.result) if (r.runner_id && r.name) names[r.runner_id] = r.name
    }
  }
  return names
}

export const hasEscalation = (run) =>
  run.steps.some((s) => s.tool === 'escalate' || s.event?.action === 'escalation_created')

// The model's final reply is free text and sometimes arrives as markdown with
// bold, backticks and bullet lists, or as a long block of reasoning. The feed
// shows it verbatim, so clean it to short plain lines: strip markdown, keep the
// "Records as of …" summary, and drop per-dose reasoning bullets the steps
// above already cover.
export function cleanReply(reply = '') {
  const stripMd = (s) => stripMarkdown(s).replace(/\s+/g, ' ').trim()

  // Split into lines, drop empty and per-dose reasoning bullets (Dose N …, Room … skip).
  const lines = reply
    .split(/\r?\n/)
    .map(stripMd)
    .filter(Boolean)
    .filter((l) => !/^dose\s*\d/i.test(l))
    .filter((l) => !/skip\b/i.test(l))

  // Prefer the summary sentence (the one that states the outcome); fall back to
  // the first/last non-bullet line. Keep it to at most two short lines.
  const summary = lines.filter((l) => !/^records as of/i.test(l))
  const records = lines.find((l) => /^records as of/i.test(l))
  const kept = [records, summary[summary.length - 1]].filter(Boolean)
  return (kept.length ? kept : lines).slice(0, 2)
}

// One step as { text, result, tone }: tone 'neg' marks what a human should notice.
export function describeStep(step, names = {}) {
  if (step.event) return describeEvent(step.event, names)
  const input = step.input || {}
  const r = step.result
  let text = humanize(step.tool || 'tool')
  let result = ''
  let tone = null
  switch (step.tool) {
    case 'get_doses_due': {
      text = input.room ? `Looked up the ${input.med || 'dose'} due for room ${input.room}` : 'Checked every dose due in the next hour'
      const doses = r?.doses
      if (Array.isArray(doses)) result = doses.length ? doses.map((d) => `${d.med} for ${d.room} at ${clock(d.due_at)}`).join(' · ') : 'Nothing due'
      break
    }
    case 'find_stock':
      text = `Looked for ${input.med} on every unit`
      if (Array.isArray(r)) {
        result = r.length ? r.map((s) => `${s.location} has ${s.qty}`).join(' · ') : 'Not in stock anywhere'
        if (!r.length) tone = 'neg'
      }
      break
    case 'get_runners':
      text = 'Checked which runners are free'
      if (Array.isArray(r)) result = r.map((x) => `${x.name || x.runner_id}: ${x.workload ? plural(x.workload, 'active delivery', 'active deliveries') : 'free'}`).join(' · ')
      break
    case 'create_delivery':
      text = `Sent ${runnerName(input.runner_id, names)} for ${input.med}, ${input.from_location} to room ${input.room}`
      if (r?.status) result = `${DELIVERY_STATUS[r.status] || r.status}, due ${clock(r.due_at || input.due_at)}`
      break
    case 'send_email':
      text = `Emailed ${runnerName(input.runner_id, names)}`
      result = [input.subject && `“${input.subject}”`, r?.status === 'dry_run' ? 'dry run, not sent' : r?.status].filter(Boolean).join(' · ')
      break
    case 'escalate':
      text = `Escalated to the ${input.to_role || 'charge'} nurse`
      result = input.reason || ''
      tone = 'neg'
      break
    case 'get_my_patients':
      text = 'Opened this nurse’s patient list'
      if (Array.isArray(r)) result = plural(r.length, 'patient')
      break
    case 'get_patient':
      text = `Opened the record for room ${input.room}`
      break
    case 'check_recent_doses':
      text = `Checked recent ${input.med} doses for room ${input.room}`
      if (Array.isArray(r)) {
        result = r.length ? `Already given by ${r[0].given_by} at ${clock(r[0].ts)}` : 'None in the last 4 hours'
        if (r.length) tone = 'neg'
      }
      break
    case 'log_event':
      text = `Logging ${humanize(input.event_type || 'event').toLowerCase()} for room ${input.room}`
      if (r?.status === 'stop') { result = `STOP: blocked a repeat dose (given by ${r.given_by} at ${clock(r.ts)})`; tone = 'neg' }
      else if (r?.status === 'logged') result = 'Logged'
      else if (r?.message) result = r.message
      break
    case 'request_med':
      text = `Requested ${input.med} for room ${input.room}`
      if (r?.status === 'requested') result = 'Sent to the routing agent'
      else if (r?.message) result = r.message
      break
    case 'get_delivery_status':
      text = `Checked deliveries to room ${input.room}`
      if (Array.isArray(r)) result = r.length ? r.map((d) => `${d.med}: ${DELIVERY_STATUS[d.status] || d.status}`).join(' · ') : 'None'
      break
    default:
      break
  }
  if (step.error) {
    result = step.error === 'PatientAccessDenied' ? 'Refused: not this nurse’s patient' : `Failed: ${step.error}`
    tone = 'neg'
  } else if (!step.done) {
    result = 'Working…'
  }
  return { text, result, tone }
}

function describeEvent({ action, detail = {} }) {
  switch (action) {
    case 'escalation_created':
      return { text: `Escalated to the ${detail.to_role || 'charge'} nurse`, result: detail.reason || '', tone: 'neg' }
    case 'event_publish_failed':
      return { text: `Couldn’t publish ${detail.detail_type}`, result: detail.error_type || '', tone: 'neg' }
    case 'scan_dose_unhandled':
      return { text: `Retrying a dose the scan missed: ${detail.med} for room ${detail.room}`, result: `Due ${clock(detail.due_at)}`, tone: null }
    case 'scan_retry_failed':
      return { text: `Retry failed: ${detail.med} for room ${detail.room}`, result: detail.error_type || '', tone: 'neg' }
    case 'agent_failed':
      return { text: 'Agent failed', result: detail.error_type || '', tone: 'neg' }
    default:
      return { text: humanize(action), result: '', tone: null }
  }
}
