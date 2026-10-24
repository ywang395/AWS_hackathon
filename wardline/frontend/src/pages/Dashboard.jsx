// Charge nurse's dashboard. GET /dashboard every 3 s (CLAUDE.md section 7).
// The screen's question: what needs a human right now, and what are the agents doing?
// Escalations lead in a full-width strip that is always there (so a new one never moves the page
// further than its own row); the agent feed is the largest block under it.

import { useEffect, useMemo, useRef, useState } from 'react'
import Shell from '../components/Shell.jsx'
import Icon from '../components/Icon.jsx'
import DeliveryPill from '../components/DeliveryPill.jsx'
import { getDashboard, isMock, markDelivered } from '../api.js'
import { useNow, usePoll } from '../lib/hooks.js'
import { age, clock, clockSeconds, duration, isOverdue, localizeTimes, parseTs, plural, relative, runnerName } from '../lib/format.js'
import { agentLabel, cleanReply, describeStep, groupRuns, hasEscalation, runnerNames } from '../lib/feed.js'

export default function Dashboard() {
  const { data, error, updatedAt, refresh } = usePoll(getDashboard, 3000)
  const now = useNow(1000)
  const feed = data?.feed || []
  const runs = useMemo(() => groupRuns(feed), [feed])
  const names = useMemo(() => runnerNames(feed), [feed])
  const down = data?.system?.status === 'down'

  // The status pulses are loops: stop them while the tab is hidden.
  useEffect(() => {
    const sync = () => document.documentElement.classList.toggle('page-hidden', document.hidden)
    sync()
    document.addEventListener('visibilitychange', sync)
    return () => document.removeEventListener('visibilitychange', sync)
  }, [])

  return (
    <Shell current="dashboard">
      <SystemBanner system={data?.system} now={now} loading={!data && !error} />
      <header className="title-row">
        <div>
          <h1>{down ? 'Unit 4 downtime' : 'Unit 4'}</h1>
          <p className="sub" aria-live="polite">{data ? finding(data) : ' '}</p>
        </div>
        <Freshness updatedAt={updatedAt} error={error} onRetry={refresh} />
      </header>
      <EscalationsPanel items={data?.escalations} down={down} now={now} />
      <div className="board">
        <FeedPanel runs={runs} names={names} loading={!data && !error} failed={!data && error} working={down && !error} offline={Boolean(error)} />
        <div className="side">
          <StaffPanel staff={data?.staff} down={down} />
          <DeliveriesPanel items={data?.deliveries} names={names} now={now} onChange={refresh} />
        </div>
      </div>
    </Shell>
  )
}

// The one line under the title: what needs attention, computed from the same data the panels show.
function finding({ system, staff = [], escalations = [], deliveries = [] }) {
  if (system?.status !== 'down') return 'Hospital systems are up. Wardline will alert every on-shift nurse if they go down.'
  const acked = staff.filter((s) => s.acked).length
  const moving = deliveries.filter((d) => d.status !== 'delivered').length
  return [
    escalations.length ? `${plural(escalations.length, 'escalation')} ${escalations.length === 1 ? 'needs' : 'need'} a human` : 'Nothing needs a human yet',
    `${acked} of ${staff.length} nurses confirmed`,
    `${plural(moving, 'delivery', 'deliveries')} under way`,
  ].join(' · ')
}

function SystemBanner({ system, now, loading }) {
  if (loading || !system) {
    return (
      <div className="banner" role="status">
        <Icon name="clock" /> <strong>Checking hospital system status…</strong>
      </div>
    )
  }
  if (system.status === 'down') {
    const since = parseTs(system.outage_started_at)
    return (
      <div className="banner banner-down" role="status">
        <span className="banner-main">
          <Icon name="alert" className="neg" />
          <strong>Hospital systems down</strong>
          {since && <span className="num">since {clock(since)} · {duration(now - since)}</span>}
        </span>
        <span className="banner-side num">
          <Icon name="clock" /> Records as of {clock(system.snapshot_ts)} · {age(system.snapshot_ts, now)}
        </span>
      </div>
    )
  }
  return (
    <div className="banner banner-up" role="status">
      <span className="banner-main">
        <Icon name="check" className="pos" />
        <strong>Hospital systems up</strong>
        <span>Wardline is standing by</span>
      </span>
    </div>
  )
}

function Freshness({ updatedAt, error, onRetry }) {
  if (error) {
    return (
      <p className="stamp stamp-error" role="alert">
        <Icon name="alert" size={16} className="neg" />
        Couldn’t reach Wardline{updatedAt ? `. Showing data from ${clockSeconds(updatedAt)}` : ''}.
        <button type="button" className="text-btn" onClick={onRetry}>Retry</button>
      </p>
    )
  }
  return <p className="stamp num">{updatedAt ? `Updated ${clockSeconds(updatedAt)} · every 3 s${isMock ? ' · mock data' : ''}` : 'Loading…'}</p>
}

// ---------------------------------------------------------------------------
// The agent feed: each run is one agent's reasoning, step by step
// ---------------------------------------------------------------------------

// A status dot: still for a settled state, pulsing while something is under way. The words next to
// it always say the same thing, so the dot is never the only signal.
function Dot({ tone = 'muted', pulse = false, burst = false }) {
  return <span className={`dot dot-${tone}${pulse ? ' dot-pulse' : ''}${burst ? ' dot-burst' : ''}`} aria-hidden="true" />
}

// Rows that arrive after the first successful load (whatever it showed, even nothing) stay marked
// for ARRIVAL_MS, long enough for their fade to finish across the screen's one-second re-renders.
const ARRIVAL_MS = 1500

function useArrivals(ids, ready) {
  const seen = useRef(null)
  const arrivedAt = useRef(new Map())
  const now = Date.now()
  if (seen.current) for (const id of ids) if (!seen.current.has(id) && !arrivedAt.current.has(id)) arrivedAt.current.set(id, now)
  useEffect(() => { if (ready) seen.current = new Set(ids) })
  return new Set(ids.filter((id) => now - (arrivedAt.current.get(id) ?? -Infinity) < ARRIVAL_MS))
}

const RECENT_RUN_LIMIT = 4

function FeedPanel({ runs, names, loading, failed, working, offline }) {
  const [opened, setOpened] = useState(() => new Set())
  const [view, setView] = useState('log')
  const shownRuns = runs.slice(0, RECENT_RUN_LIMIT)
  const toggle = (id) => setOpened((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const arrived = useArrivals(shownRuns.map((r) => r.id), !loading && !failed)
  return (
    <section className="panel feed" aria-labelledby="feed-title">
      <header className="feed-header">
        <div>
          <h2 id="feed-title">Agent activity</h2>
          <span className="meta">
            {offline
              ? <><Dot tone="neg" /> Not updating</>
              : <><Dot tone="pos" pulse={working} /> {working ? 'Agents working' : 'Live'}{shownRuns.length ? ` · ${shownRuns.length} recent` : ''}</>}
          </span>
        </div>
        <div className="view-switch" role="group" aria-label="Agent activity view">
          {['log', 'cards'].map((option) => (
            <button
              key={option}
              type="button"
              className="view-option"
              aria-pressed={view === option}
              onClick={() => setView(option)}
            >
              {option === 'log' ? 'Log' : 'Cards'}
            </button>
          ))}
        </div>
      </header>
      <div className="feed-scroll">
        {loading && <SkeletonRows count={4} tall />}
        {failed && <p className="empty">Couldn’t load the agent feed. It will retry every 3 seconds.</p>}
        {!loading && !failed && !runs.length && (
          <p className="empty">No agent activity yet. When systems go down, every agent decision appears here as it happens.</p>
        )}
        <ol className={`runs runs-${view}`} aria-label={`${shownRuns.length} most recent agent activities`}>
          {shownRuns.map((run) => <Run key={run.id} run={run} names={names} isNew={arrived.has(run.id)} opened={opened.has(run.id)} onToggle={() => toggle(run.id)} />)}
        </ol>
      </div>
    </section>
  )
}

// A finished run that needed nobody collapses to its outcome; working, failed and flagged runs
// (an escalation, a STOP, a refusal) stay open, because those are the ones to read.
function Run({ run, names, isNew, opened, onToggle }) {
  const arrive = isNew ? ' run-new' : ''
  if (run.status === 'single') {
    const { text, result, tone } = describeStep(run.steps[0], names)
    return (
      <li className={`run run-single${tone === 'neg' ? ' run-flag' : ''}${arrive}`}>
        <time className="run-time num" dateTime={run.started}>{clockSeconds(run.started)}</time>
        <div className="run-body">
          <p className="run-head"><span className="run-agent"><Dot tone={tone === 'neg' ? 'neg' : 'muted'} /><strong>{agentLabel(run.agent)}</strong></span></p>
          <Step text={text} result={result} tone={tone} at={run.started} />
        </div>
      </li>
    )
  }
  const flagged = hasEscalation(run)
  const described = run.steps.map((step) => ({ id: step.id, ...describeStep(step, names) }))
  const notable = flagged || run.status !== 'done' || described.some((d) => d.tone === 'neg')
  const open = notable || opened
  const secs = run.ended ? Math.max(0, Math.round((parseTs(run.ended) - parseTs(run.started)) / 1000)) : null
  return (
    <li className={`run${flagged ? ' run-flag' : ''}${arrive}`}>
      <time className="run-time num" dateTime={run.started}>{clockSeconds(run.started)}</time>
      <div className="run-body">
        <p className="run-head">
          <span className="run-agent">
            <Dot tone={run.status === 'failed' || flagged ? 'neg' : 'pos'} pulse={run.status === 'running'} />
            <strong>{agentLabel(run.agent)}</strong>
          </span>
          <RunStatus run={run} secs={secs} flagged={flagged} />
        </p>
        {open && (
          <ol className="steps" id={`steps-${run.id}`}>
            {described.map((d) => <li key={d.id}><Step {...d} at={run.started} /></li>)}
          </ol>
        )}
        {run.reply && cleanReply(run.reply).map((line, i) => (
          <p key={i} className="run-reply">{localizeTimes(line, run.started)}</p>
        ))}
        {!notable && run.steps.length > 0 && (
          <button type="button" className="text-btn run-toggle" aria-expanded={open} aria-controls={`steps-${run.id}`} onClick={onToggle}>
            {open ? 'Hide checklist' : `Show checklist (${run.steps.length})`}
          </button>
        )}
      </div>
    </li>
  )
}

function RunStatus({ run, secs, flagged }) {
  if (run.status === 'running') return <span className="run-status">Working · {plural(run.steps.length, 'step')} so far</span>
  if (run.status === 'failed') return <span className="run-status neg">{run.note}</span>
  return (
    <span className="run-status">
      {flagged ? 'Handed to a human · ' : 'Done · '}
      {plural(run.steps.length, 'step')}{secs !== null ? ` · ${secs} s` : ''}
    </span>
  )
}

function Step({ text, result, tone, at }) {
  return (
    <div className={`step${tone === 'neg' ? ' step-neg' : ''}`}>
      <span className="step-text">{tone === 'neg' && <Icon name="alert" size={16} className="neg" />}{localizeTimes(text, at)}</span>
      {result && <span className="step-result">{localizeTimes(result, at)}</span>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Right column
// ---------------------------------------------------------------------------

function EscalationsPanel({ items, down, now }) {
  const list = [...(items || [])].sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)))
  return (
    <section className={`panel escalations${list.length ? ' has-open' : ''}`} aria-labelledby="esc-title">
      <header>
        <h2 id="esc-title">Needs a human</h2>
        {list.length > 0 && <span className="lead num">{list.length} open</span>}
      </header>
      {!items && <SkeletonRows count={1} />}
      {items && !list.length && (
        <p className="esc-none muted">
          <Icon name="check" size={16} /> {down ? 'Nothing right now. The agents are handling every dose so far.' : 'Nothing. Systems are up.'}
        </p>
      )}
      <ul className="esc-list">
        {list.map((e) => (
          <li key={e.escalation_id} className="esc">
            <Icon name="escalate" className="neg" />
            <div>
              <p className="esc-reason">{localizeTimes(e.reason, e.created_at)}</p>
              <p className="row-meta num">
                {[e.room && `Room ${e.room}`, agentLabel(e.source_agent), `${clock(e.created_at)}${relative(e.created_at, now) ? ` (${relative(e.created_at, now)})` : ''}`].filter(Boolean).join(' · ')}
              </p>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

function StaffPanel({ staff, down }) {
  const list = [...(staff || [])].sort((a, b) => Number(a.acked) - Number(b.acked) || a.name.localeCompare(b.name))
  const acked = list.filter((s) => s.acked).length
  // Names that were waiting on the previous poll: when one confirms, its dot rings once.
  const waiting = useRef(new Set())
  const justConfirmed = new Set(list.filter((s) => s.acked && waiting.current.has(s.name)).map((s) => s.name))
  useEffect(() => { waiting.current = new Set(down ? list.filter((s) => !s.acked).map((s) => s.name) : []) })
  return (
    <section className="panel" aria-labelledby="staff-title">
      <header>
        <h2 id="staff-title">Nurses reached</h2>
        <span className="lead num">{staff && down ? `${acked} of ${list.length}` : ''}</span>
      </header>
      {staff && down && list.length > 0 && (
        <div className="reach-track" aria-hidden="true">
          <span className="reach-fill" style={{ transform: `scaleX(${acked / list.length})` }} />
        </div>
      )}
      {!staff && <SkeletonRows count={4} />}
      {staff && !list.length && <p className="empty">No nurses on shift in the record.</p>}
      <ul className="rows">
        {list.map((s) => (
          <li key={s.name} className="row">
            <Icon name="person" className="muted" />
            <span className="row-name">{s.name}</span>
            {!down ? (
              <span className="pill"><Dot /> Not alerted</span>
            ) : s.acked ? (
              <span className="pill pos"><Dot tone="pos" burst={justConfirmed.has(s.name)} /> Confirmed</span>
            ) : (
              <span className="pill neg"><Dot tone="neg" pulse /> No reply yet</span>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

const ROWS_SHOWN = 4

const UNDO_SECONDS = 8

function DeliveriesPanel({ items, names, now, onChange }) {
  const [all, setAll] = useState(false)
  const [pending, setPending] = useState({}) // request_id -> ms when it will be sent
  const [failed, setFailed] = useState(null)
  const timers = useRef({})
  const list = [...(items || [])].sort(
    (a, b) => Number(a.status === 'delivered') - Number(b.status === 'delivered') || String(a.due_at).localeCompare(String(b.due_at)),
  )
  const moving = list.filter((d) => d.status !== 'delivered' && !pending[d.request_id]).length
  const shown = all ? list : list.slice(0, ROWS_SHOWN)

  async function send(id, keepalive = false) {
    clearTimeout(timers.current[id])
    delete timers.current[id]
    try {
      await markDelivered(id, { keepalive })
      if (!keepalive) await onChange()
    } catch {
      setFailed(id)
    } finally {
      setPending(({ [id]: _, ...rest }) => rest)
    }
  }

  // The API has no undo, so the call waits UNDO_SECONDS behind an Undo button.
  function done(id) {
    setFailed(null)
    setPending((p) => ({ ...p, [id]: Date.now() + UNDO_SECONDS * 1000 }))
    timers.current[id] = setTimeout(() => send(id), UNDO_SECONDS * 1000)
  }

  function undo(id) {
    clearTimeout(timers.current[id])
    delete timers.current[id]
    setPending(({ [id]: _, ...rest }) => rest)
  }

  // Leaving the page commits what is waiting, so a tap is never silently lost.
  useEffect(() => {
    const flush = () => { for (const id of Object.keys(timers.current)) send(id, true) }
    window.addEventListener('pagehide', flush)
    return () => window.removeEventListener('pagehide', flush)
  })

  return (
    <section className="panel" aria-labelledby="del-title">
      <header>
        <h2 id="del-title">Deliveries</h2>
        <span className="lead num">{items ? `${moving} under way` : ''}</span>
      </header>
      {!items && <SkeletonRows count={2} />}
      {items && !list.length && <p className="empty">No deliveries yet. Runners are dispatched when a dose due within the hour isn’t on the unit.</p>}
      <ul className="rows">
        {shown.map((d) => {
          const late = d.status !== 'delivered' && isOverdue(d.due_at, now)
          return (
            <li key={d.request_id} className="row delivery">
              <div className="delivery-main">
                <p className="delivery-title">
                  <span className="row-name">{d.med} <span className="muted">to</span> room {d.to_room}</span>
                  <span className="delivery-due num">due {clock(d.due_at)}</span>
                </p>
                <p className="row-meta">{[`From ${d.from_location}`, runnerName(d.runner_id, names), d.priority && d.priority[0].toUpperCase() + d.priority.slice(1)].filter(Boolean).join(' · ')}</p>
                {failed === d.request_id && <p className="row-meta neg" role="alert">Couldn’t mark it delivered. Try again.</p>}
              </div>
              <div className="delivery-side">
                {pending[d.request_id] ? (
                  <>
                    <span className="pill pos"><Icon name="check" size={16} /> Delivered · saving in {Math.max(0, Math.ceil((pending[d.request_id] - now) / 1000))} s</span>
                    <button type="button" className="btn" onClick={() => undo(d.request_id)}>Undo</button>
                  </>
                ) : (
                  <>
                    <DeliveryPill status={d.status} late={late} />
                    {d.status !== 'delivered' && (
                      <button type="button" className="btn" onClick={() => done(d.request_id)}>Mark delivered</button>
                    )}
                  </>
                )}
              </div>
            </li>
          )
        })}
      </ul>
      {list.length > ROWS_SHOWN && (
        <button type="button" className="text-btn" onClick={() => setAll(!all)}>
          {all ? 'Show fewer' : `Show all ${list.length}`}
        </button>
      )}
    </section>
  )
}

function SkeletonRows({ count, tall }) {
  return (
    <div className="skeletons" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => <div key={i} className={`skeleton-row${tall ? ' tall' : ''}`}><span className="skeleton" /><span className="skeleton short" /></div>)}
    </div>
  )
}
