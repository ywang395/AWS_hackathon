// Charge nurse's dashboard. GET /dashboard every 3 s (CLAUDE.md section 7).
// The screen's question: what are the agents doing, and what needs a human right now?
// The agent feed is the largest block; escalations lead the right column.

import { useMemo, useState } from 'react'
import Shell from '../components/Shell.jsx'
import Icon from '../components/Icon.jsx'
import DeliveryPill from '../components/DeliveryPill.jsx'
import { getDashboard, isMock, markDelivered } from '../api.js'
import { useNow, usePoll } from '../lib/hooks.js'
import { age, clock, clockSeconds, duration, isOverdue, localizeTimes, parseTs, plural, relative, runnerName } from '../lib/format.js'
import { agentLabel, describeStep, groupRuns, hasEscalation, runnerNames } from '../lib/feed.js'

export default function Dashboard() {
  const { data, error, updatedAt, refresh } = usePoll(getDashboard, 3000)
  const now = useNow(1000)
  const feed = data?.feed || []
  const runs = useMemo(() => groupRuns(feed), [feed])
  const names = useMemo(() => runnerNames(feed), [feed])
  const down = data?.system?.status === 'down'

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
      <div className="board">
        <FeedPanel runs={runs} names={names} loading={!data && !error} failed={!data && error} />
        <div className="side">
          <EscalationsPanel items={data?.escalations} down={down} now={now} />
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

function FeedPanel({ runs, names, loading, failed }) {
  const events = runs.reduce((n, r) => n + Math.max(1, r.steps.length), 0)
  return (
    <section className="panel feed" aria-labelledby="feed-title">
      <header>
        <h2 id="feed-title">Agent activity</h2>
        <span className="meta">
          <Icon name="activity" size={16} /> {runs.length ? `${plural(runs.length, 'run')} · ${plural(events, 'step')}` : 'Live'}
        </span>
      </header>
      <div className="feed-scroll">
        {loading && <SkeletonRows count={5} tall />}
        {failed && <p className="empty">Couldn’t load the agent feed. It will retry every 3 seconds.</p>}
        {!loading && !failed && !runs.length && (
          <p className="empty">No agent activity yet. When systems go down, every agent decision appears here as it happens.</p>
        )}
        <ol className="runs">
          {runs.map((run) => <Run key={run.id} run={run} names={names} />)}
        </ol>
      </div>
    </section>
  )
}

function Run({ run, names }) {
  if (run.status === 'single') {
    const { text, result, tone } = describeStep(run.steps[0], names)
    return (
      <li className={`run run-single${tone === 'neg' ? ' run-flag' : ''}`}>
        <time className="run-time num" dateTime={run.started}>{clockSeconds(run.started)}</time>
        <div className="run-body">
          <p className="run-head"><strong>{agentLabel(run.agent)}</strong></p>
          <Step text={text} result={result} tone={tone} at={run.started} />
        </div>
      </li>
    )
  }
  const flagged = hasEscalation(run)
  const secs = run.ended ? Math.max(0, Math.round((parseTs(run.ended) - parseTs(run.started)) / 1000)) : null
  return (
    <li className={`run${flagged ? ' run-flag' : ''}`}>
      <time className="run-time num" dateTime={run.started}>{clockSeconds(run.started)}</time>
      <div className="run-body">
        <p className="run-head">
          <strong>{agentLabel(run.agent)}</strong>
          <RunStatus run={run} secs={secs} flagged={flagged} />
        </p>
        <ol className="steps">
          {run.steps.map((step) => {
            const d = describeStep(step, names)
            return <li key={step.id}><Step {...d} at={run.started} /></li>
          })}
        </ol>
        {run.reply && <p className="run-reply">{localizeTimes(run.reply, run.started)}</p>}
      </div>
    </li>
  )
}

function RunStatus({ run, secs, flagged }) {
  if (run.status === 'running') return <span className="run-status"><Icon name="clock" size={16} /> Working · {plural(run.steps.length, 'step')} so far</span>
  if (run.status === 'failed') return <span className="run-status neg"><Icon name="alert" size={16} /> {run.note}</span>
  return (
    <span className="run-status">
      {flagged ? <><Icon name="escalate" size={16} className="neg" /> Handed to a human · </> : <><Icon name="check" size={16} /> Done · </>}
      {plural(run.steps.length, 'step')}{secs !== null ? ` · ${secs} s` : ''}
    </span>
  )
}

// Agents write times as ISO or "HH:MM UTC"; `at` dates them so they read in local time.
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
    <section className="panel" aria-labelledby="esc-title">
      <header>
        <h2 id="esc-title">Needs a human</h2>
        <span className="lead num">{items ? `${list.length} open` : ''}</span>
      </header>
      {!items && <SkeletonRows count={2} />}
      {items && !list.length && (
        <p className="empty">{down ? 'Nothing needs a human right now. The agents are handling every dose so far.' : 'No escalations.'}</p>
      )}
      <ul className="rows">
        {list.map((e) => (
          <li key={e.escalation_id} className="row esc">
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
  return (
    <section className="panel" aria-labelledby="staff-title">
      <header>
        <h2 id="staff-title">Nurses reached</h2>
        <span className="lead num">{staff && down ? `${acked} of ${list.length}` : ''}</span>
      </header>
      {!staff && <SkeletonRows count={4} />}
      {staff && !list.length && <p className="empty">No nurses on shift in the record.</p>}
      <ul className="rows">
        {list.map((s) => (
          <li key={s.name} className="row">
            <Icon name="person" className="muted" />
            <span className="row-name">{s.name}</span>
            {!down ? (
              <span className="pill">Not alerted</span>
            ) : s.acked ? (
              <span className="pill pos"><Icon name="check" size={16} /> Confirmed</span>
            ) : (
              <span className="pill warn"><Icon name="clock" size={16} /> No reply yet</span>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

const ROWS_SHOWN = 4

function DeliveriesPanel({ items, names, now, onChange }) {
  const [all, setAll] = useState(false)
  const [busy, setBusy] = useState(null)
  const [failed, setFailed] = useState(null)
  const list = [...(items || [])].sort(
    (a, b) => Number(a.status === 'delivered') - Number(b.status === 'delivered') || String(a.due_at).localeCompare(String(b.due_at)),
  )
  const moving = list.filter((d) => d.status !== 'delivered').length
  const shown = all ? list : list.slice(0, ROWS_SHOWN)

  async function done(id) {
    setBusy(id)
    setFailed(null)
    try {
      await markDelivered(id)
      await onChange()
    } catch {
      setFailed(id)
    } finally {
      setBusy(null)
    }
  }

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
                <p className="row-meta">{[`From ${d.from_location}`, runnerName(d.runner_id, names), d.priority].filter(Boolean).join(' · ')}</p>
                {failed === d.request_id && <p className="row-meta neg" role="alert">Couldn’t mark it delivered. Try again.</p>}
              </div>
              <div className="delivery-side">
                <DeliveryPill status={d.status} late={late} />
                {d.status !== 'delivered' && (
                  <button type="button" className="btn" disabled={busy === d.request_id} onClick={() => done(d.request_id)}>
                    {busy === d.request_id ? 'Saving…' : 'Mark delivered'}
                  </button>
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
